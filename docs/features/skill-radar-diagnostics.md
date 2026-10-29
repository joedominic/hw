# Skill Radar & Fit Diagnostics (Dedicated Pipeline Engine)

The **Skill Radar & Fit Diagnostics** subsystem extracts deep technical competencies, stretch skill gaps, realistic interview callback probabilities, and an executive rationale comparing a candidate's track-bound resume to a target job description in the Career Cockpit.

### Architectural Consolidation: Skill Radar as Unified Pipeline Engine

To eliminate divergent scoring between background queues and manual Cockpit triage, **Skill Radar (`skill_radar`)** is the unified candidate-job fit evaluation engine for all pipeline vetting, search match APIs, and Career Cockpit diagnostics. The legacy **"Job Matching Scorer" (`matching`)** has been retired.

| Feature / Domain | Subsystem | Prompt Template | Query Kind | Engine Policy |
| :--- | :--- | :--- | :--- | :--- |
| **Pipeline Vetting, AI Match & Cockpit** | `skill_radar` | `DEFAULT_SKILL_RADAR_SYSTEM` | `USAGE_QUERY_SKILL_RADAR` (`skill_radar`) | **Local SLM Preferred (Ollama Local)** with **Cloud SLM-Only Fallback**. Frontier LLMs are strictly forbidden. |
| **Resume Tailoring (Optimizer)** | `resume_judge` | `DEFAULT_RESUME_JUDGE_SYSTEM` | `USAGE_QUERY_RESUME_JUDGE` (`resume_judge`) | **Cloud LLMs** (`allow_local=False`) for deep dual-lens ATS + Recruiter feedback. |

- **Unified Single Source of Truth**: Background pipeline vetting (`tasks.evaluate_vetting_matching_task`), on-demand search matching (`jobs_api.match_selected_jobs`), and Career Cockpit diagnostics route through `SkillRadarService.analyze()`, ensuring 100% consistency in match scores, calibrated probabilities, core competencies, and stretch skill gaps.
- **LLM Gateway SLM Tier Enforcement & UI Configuration**:
  To protect against cloud token burn while giving users complete transparency and control:
  - **UI Tier Selector (`Settings -> LLM Providers & Preference Order`)**: Every configured model row includes a dedicated **Tier** dropdown (`SLM` vs `Frontier`).
  - **Smart Auto-Detection with Manual Override**: When selecting a model in the UI, the frontend auto-suggests the recommended tier, but the user can manually override it for any custom, fine-tuned, or OpenRouter model.
  - **Priority-Based Resolution Among Multiple SLMs**: When multiple models are marked as `SLM` (e.g., Ollama Local @ Priority 0, Gemini 2.0 Flash @ Priority 1, GPT-4o-mini @ Priority 2), the gateway evaluates them strictly in ascending `priority` order. The lowest priority number is attempted first; subsequent SLMs are only invoked if earlier SLMs fail, time out, or encounter rate limits.
  - **Permitted SLMs**: Local Ollama instances and any Cloud model explicitly designated as `SLM` (e.g. Google Gemini 1.5/2.0 Flash, OpenAI GPT-4o-mini, Anthropic Claude 3.5 Haiku, Groq 8B).
  - **Blocked Frontier Models**: Models designated as `Frontier` (e.g. GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro) are strictly rejected by the gateway for evaluation queries (`max_model_tier="slm"`), guaranteeing that automated background vetting and search matching can never burn Frontier token budgets.
- **SLM Performance Tuning & Anti-Timeout Controls**:
  - **KV-Cache Prefix Preservation & Deterministic Order**:
    - **System Prompt Integrity**: All static task instructions, evaluation rules, categorical definitions, noise reduction guidelines, and JSON schemas reside strictly within `DEFAULT_SKILL_RADAR_SYSTEM`.
    - **Resume Prefix Ordering**: `DEFAULT_SKILL_RADAR_USER` places `<CANDIDATE_RESUME>{resume_text}</CANDIDATE_RESUME>` before the dynamic job description `[TARGET JOB: {job_title}]\n{job_description}`. This preserves a warm KV cache in the local SLM across batch and sequential job evaluations for the candidate (~4,750 tokens/sec cache reuse).
    - **Runtime Invariant Guard (`_ensure_kv_cache_prefix_order`)**: Dynamically verifies in `SkillRadarService.analyze()` that `{resume_text}` precedes `{job_description}`, prepending the resume if a custom user prompt flips the order.
  - **Primary Local SLM: Qwen 3.8B Distill**: The default model for `Ollama Local` is set to `hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M` (Priority 10). Empirical benchmarking demonstrated that Qwen3.8-4B decisively captures multi-framework knockout gaps (e.g., PCI and ISO/NIST mandates) without token smearing, outperforming Nemotron-3-Nano while running within the same ~2.3 GB VRAM footprint.
  - **Speculative Thinking Disabled & 2048 Token Completion Ceiling**: For local reasoning SLMs, `think=False` and `reasoning_effort="none"` are enforced by default (`OLLAMA_ENABLE_THINKING=False` in `.env`). The completion token ceiling in `factory.py` is expanded to **2048 tokens** so that large frontier or reasoning models (e.g. `gpt-oss:120b`, DeepSeek-R1) that emit internal reasoning/CoT tokens have ample headroom to complete their reasoning phase and output the structured JSON object into `content` without truncation.
  - **Autoregressive Commitment Trap Prevention (Inverted Prompt Ordering)**: `DEFAULT_SKILL_RADAR_SYSTEM` places `qualification_gaps` **before** `core_competencies` in both evaluation rules and output JSON schemas. This forces autoregressive models to identify missing domain requirements first, preventing the model from falsely committing to hallucinated competencies.
  - **Conversational Prefix Stripping & Gap Length Expansion**: `clean_skill_string` automatically strips conversational negative prefixes (e.g. "No demonstrated experience with...", "No explicit...", "Lack of...") down to the core noun phrase while preserving technical names like `.NET`. `is_valid_competency` allows qualifications up to 90 characters to support complex compliance and platform standards.
  - **LiteLLM Localhost Connection Bypass (16s Latency Fix)**:
    - Ollama models are dynamically registered in `litellm.model_cost` via `litellm.register_model` in `factory.py`, preventing LiteLLM from making slow, blocking `/api/show` HTTP queries to `localhost:11434` when the server is on a LAN host.
    - Synchronizes `os.environ["OLLAMA_API_BASE"] = api_base` to prevent socket timeout delays.
  - **Opportunity Noise & Structural Gap Reduction (Knockout vs. Secondary Gaps)**:
    - Enforces a clear hierarchy of gaps for qualification evaluation across all professional industries (Tech, Healthcare, Finance, Legal, Construction, etc.):
      - **Knockout Gaps (`knockout_gaps`)**: Mandatory, non-negotiable structural requirements (core runtime languages, essential clinical or legal licensures/certifications, required platform prerequisites, or regulatory mandates) explicitly demanded in the target posting that are completely absent in the resume.
      - **Secondary Gaps (`secondary_gaps`)**: Preferred qualifications, secondary tool deltas, or ancillary framework differences between candidate background and job requirements.
      - **Excluded Gaps**: Strictly bars generic office/workflow tools (Jira, Slack, Git, Excel) and soft skills (leadership, communication) from consuming gap slots.
      - **Backward Compatibility**: `stretch_skills` is automatically synthesized from `knockout_gaps + secondary_gaps` to ensure seamless compatibility with older UI cards and API clients.
  - **Seniority-Calibrated Competency Selection & Scope Mismatch**:
    - For Senior/Lead/Executive roles, prompts the model to prioritize high-order strategic, architectural, or domain methodologies over entry-level implementation tooling.
    - Introduces `SCOPE_MISMATCH` alignment tier: flags when a candidate operates at an executive/organizational tier significantly higher than the day-to-day execution level required by the role, or conversely when an executive role candidate lacks required hands-on day-to-day execution experience.
  - **Guided Executive Synthesis (`fit_summary`)**:
    - Directs the SLM to construct a punchy, executive-grade evaluation under 25 words stating the candidate's core anchor and single most critical gap.
    - Forbids conversational filler ('Overall,', 'This candidate is...') and unescaped quotes to safeguard against JSON syntax truncation and UI card overflow.
  - **Input Payload Compression & Heuristic Fluff Cleansing**: Resume context is capped at 4,000 characters and job descriptions are pre-cleansed via `JDCleanserService.cleanse_heuristically` to strip modern corporate fluff (benefits, "What's in it for you", hybrid work models, recruitment AI disclosures, and pay disclosures). This cuts input tokens by ~40% (down to ~1,800 tokens), preventing 4B SLM context exhaustion and latency spikes.
  - **Stop Sequence Delimiters & Resilient Bracket Balancing**:
    - **Stop Tokens**: Stop sequences in `factory.py` are streamlined to turn-terminating tokens (`<|im_end|>`, `<|end_of_text|>`, `<|eot_id|>`, `<|im_start|>`) rather than bare `}\n`. This prevents Ollama from prematurely stripping the outer closing brace `}` from the output JSON.
    - **Resilient Bracket Balancing (`_parse_llm_response`)**: `_parse_llm_response` detects and balances unclosed braces dynamically (`open_braces > close_braces`), ensuring that nested objects (e.g. `qualification_gaps: { ... }`) do not cause greedy regex truncation before `core_competencies` and `fit_summary` are read.
  - **Categorical Rubric Schema & Deterministic Scoring**:
    - **Cognitive Limitation of 4B SLMs (Seniority Valence Floor)**: Small 3B–4B parameter models cannot reliably perform multi-variable arithmetic deductions. High-seniority tokens ("Distinguished", "20 years", "Rainmaker", "$55MM") create an internal positive valence floor preventing the model from outputting numbers below 60. Moreover, shared title tokens ("Architect" == "Architect") trigger the "same engineering discipline" tier (55–75%) even when the business domain is foreign (e.g. Enterprise FinTech vs. Semiconductor Hardware).
    - **Categorical Extraction**: Instead of prompting the SLM for subjective numeric percentages, `DEFAULT_SKILL_RADAR_SYSTEM` extracts discrete categorical classifications:
      - `domain_alignment`: `"DIRECT_MATCH"` | `"ADJACENT_DOMAIN"` | `"CROSS_INDUSTRY_MISMATCH"`
        - **Shared Enterprise Software Ecosystem**: Enterprise Software, FinTech, B2B SaaS, IT Services / Solutions Consulting (e.g. NTT DATA, Accenture), Cloud Platforms, and AI Platforms ("Service-as-Software", Agentic AI, GenAI) all share the enterprise software discipline. A candidate moving between these sectors is classified as `DIRECT_MATCH` or `ADJACENT_DOMAIN`, never `CROSS_INDUSTRY_MISMATCH`.
        - **Strict Non-Software Isolation**: `CROSS_INDUSTRY_MISMATCH` is strictly quarantined for non-software shifts (Physical Semiconductor Hardware/Silicon, Embedded Firmware, Physical Robotics/Automotive Control Systems, Clinical Healthcare, Biotech Laboratory, or 3D Game Graphics Engines).
      - `seniority_alignment`: `"MATCH"` | `"UNDERQUALIFIED"` | `"OVERQUALIFIED"`
        - **Executive / Senior Scale**: For Director, Executive, Principal, Staff, or "15+ years" postings, 15 to 25+ years of experience is classified as `"MATCH"`, not `"OVERQUALIFIED"`. `"OVERQUALIFIED"` is reserved strictly for senior candidates applying down into junior or mid-level individual contributor roles.
    - **Deterministic Python Calculation (`calculate_categorical_scores`)**:
      - `DIRECT_MATCH`: Base 88% match / 82% callback probability (-3 match / -4 prob per stretch skill).
      - `ADJACENT_DOMAIN`: Base 68% match / 58% callback probability (-3 match / -4 prob per stretch skill).
      - `CROSS_INDUSTRY_MISMATCH`: Base 35% match / 18% callback probability with strict hard ceilings: **`match_score <= 42%`** and **`interview_probability <= 25%`**.
      - Seniority adjustments: `OVERQUALIFIED` (-8 match, -10 prob), `UNDERQUALIFIED` (-15 match, -20 prob).
  - **Universal Industry-Agnostic Prompt**: Framed around specialized domain platforms, certifications, tools, and methodologies rather than software-only jargon, ensuring accurate diagnostics across Healthcare, Finance, Construction, Legal, Sales, and Engineering.
  - **Strict Factual Attribution & Gap Verification**: Directly instructs the model to never extrapolate or assume credentials/tools absent from the resume, and never flag an explicit candidate skill as a stretch gap.
  - **Structural Skeleton Priming for 4B Models**: 3B–4B SLMs drift from length constraints when few-shot examples exhibit mismatched cardinality. The prompt schema explicitly primes 4 items in `core_competencies` and 2 items in `stretch_skills`, locking the autoregressive generation into the target output counts.
  - **Deterministic Ground-Truth Reconciliation (`reconcile_skills_with_ground_truth`)**:
    - **Tiered Model Execution (`is_frontier`)**:
      - **Cloud Frontier LLMs (`is_frontier=True`)**: Frontier models (GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro) demonstrate high semantic precision and understand context distinctions (e.g. an Electrical Engineering degree + Oracle GenAI cert does not satisfy a *Software Engineering Certification*). When `is_frontier=True`, heuristic false-negative promotions are disabled, preventing fuzzy string-matching from overriding the LLM's deliberate gap determinations. High-order synthesized core capabilities are preserved, qualification gaps are synchronized, and deterministic rubric scores (`calculate_categorical_scores`) are calculated.
      - **Local SLMs (`is_frontier=False`)**: Smaller on-prem models (7B/8B on Ollama) are prone to attention decay and inverted hallucinations; for SLMs, the full heuristic safety net (false-negative recovery, hallucination pruning, and domain re-anchoring) remains active.
    - **Cloud Parity Reclassification (`CLOUD_PLATFORM_KEYWORDS`)**:
      - When candidate has verified major public cloud infrastructure experience (GCP, Azure, AWS) in their resume, an alternative cloud platform requirement (e.g. AWS when candidate is a GCP Cloud Architect) is recognized as a transferable secondary platform delta rather than a fatal knockout gap.
      - Both the prompt instructions and the Python reconciler reclassify cross-cloud provider deltas from `knockout_gaps` to `secondary_gaps`, preventing multi-cloud leaders from being falsely penalized with structural knockout ceilings.
    - **Calibrated Multi-Knockout Ceilings for `DIRECT_MATCH`**:
      - For roles with `DIRECT_MATCH` domain alignment and $\ge 2$ knockout gaps, the scoring ceiling is calibrated to `match_score <= 52%` and `interview_probability <= 28%` (softened from the non-direct `45%` / `22%` threshold), recognizing that the candidate possesses direct domain and functional alignment.
    - **Credential Subject-Matter Grounding (`CREDENTIAL_INDICATORS`)**: Multi-word credential and degree requirements (e.g. *"Software Engineering Certification"*, *"Computer Science Degree"*) are protected from generic stopword stripping. Even under SLM reconciliation, a credential requirement cannot be validated solely because the single word *"certification"* appears in an unrelated resume section.
    - **False-Negative Recovery (SLM)**: If an item in `stretch_skills` (e.g. "LangChain", "ACLS", "SOX") genuinely appears in the candidate's resume text, it is recovered into `core_competencies`. To prevent generic single-word false matches (e.g. a candidate resume mentioning "API integration" falsely triggering recovery for "Hardware-Specific Firmware Integration"), multi-word phrases require all distinctive non-stopword tokens to match, and hardware/firmware skills are strictly barred from recovery if the candidate has zero hardware background.
    - **Hallucination Pruning**: If a qualification in `core_competencies` (e.g. "Python", "EPIC EHR", "NetSuite") has zero basis in the resume text, it is pruned. If the target job demanded it, it is demoted to `stretch_skills`.
    - **Collision Filtering (`is_skill_covered_by_core`)**: Drops any stretch skill candidate covered by or subsumed within core competencies.
    - **Hardware/Firmware & OEM Conglomerate Safeguards**:
      - Detects physical hardware/semiconductor/embedded requirements (e.g. silicon, firmware, FPGA, RTL, autonomous vehicles, robotics). If the target posting requires hardware disciplines and the candidate has zero hardware background in their resume, `CROSS_INDUSTRY_MISMATCH` is deterministically enforced, capping match score $\le 40\%$ and callback probability $\le 20\%$.
      - **Hardware OEM Company Grounding (`HARDWARE_SYSTEMS_COMPANIES`)**: For semiconductor and hardware OEMs (e.g. Broadcom, Nvidia, Qualcomm, Intel, AMD, Marvell), software candidates without hardware background are strictly prevented from claiming an uncalibrated `DIRECT_MATCH`; the reconciler automatically re-anchors to `ADJACENT_DOMAIN` to reflect transferable software engineering into an adjacent hardware ecosystem.
    - **Software Domain Misclassification Safeguard**: If the SLM hallucinates a `CROSS_INDUSTRY_MISMATCH` on an enterprise cloud/AI SaaS posting (with no hardware or clinical requirements) for a candidate with verified enterprise software/AI core competencies, the reconciler automatically re-anchors the classification to `DIRECT_MATCH` or `ADJACENT_DOMAIN` and recalibrates scores, preventing false-negative rejections.
    - **Corporate Parenthetical Sanitization (`_sanitize_resume_for_skills`)**:
      - Strips acquisition notices and employer corporate parentheticals (e.g. `(Acquired by SAP)`, `(formerly...)`, `(division of...)`) from candidate resume text during technical skill grounding.
      - Prevents vendor suites (e.g., SAP, Salesforce, Oracle) from being falsely promoted to verified core competencies purely because a past employer was acquired by that vendor.
    - **People Leadership & Talent Development Protection (`_has_verified_people_leadership`)**:
      - Substantive executive/director management records (e.g., directing 95-member organizations, managing managers, team scaling) are recognized as verified leadership grounding.
      - Protects functional capabilities like `Engineering Organization Leadership & Talent Development` from being falsely demoted to stretch gaps due to missing buzzword nouns (e.g. `"talent"`), preserving the model's original executive rationale.
    - **Cross-Sector Operational Domain Guard (`SUPPLY_CHAIN_LOGISTICS_KEYWORDS`)**:
      - When a job focuses on physical logistics/supply chain platforms (`Supply Chain`, `Logistics`, `Warehouse`, `WMS`, `Fulfillment`) and the candidate's verified background is in `Financial Services / Banking Compliance`, an uncalibrated `DIRECT_MATCH` is re-anchored to `ADJACENT_DOMAIN` (same software discipline in an adjacent business sector).
    - **Morphological Token Stemming (`_token_matches`)**:
      - Supports suffix inflection matching (`leads`/`leader`/`leadership`, `manage`/`management`, `organize`/`organizational`) so valid variations are not discarded by strict token equality.
    - **Deduplication & Subsumption Guard (`is_duplicate_or_subsumed`)**: Evaluates non-stopword token overlap and string containment across `stretch_skills`, `knockout_gaps`, and `secondary_gaps`. Any repeated or near-duplicate phrases emitted by small models (e.g. repeating `"Broadcom-specific R&D domain context"`) are collapsed into single distinct qualifications.
    - **Tech IC Leveling & Banking Rank Calibrations**:
      - Tech IC bands (`Engineer Software 5`, `SWE Level 5`, `IC5-7`, `Level V-VII`) are recognized as `staff_principal` individual contributor tiers.
      - Recognizes financial services corporate rank norms: Executive Directors or VPs in Architecture/Engineering with substantiated hands-on technical architecture (Kafka, Kubernetes, LangGraph, Python) are evaluated without false managerial `SCOPE_MISMATCH` penalties when applying to high-band Staff/Principal technical IC roles.
  - **Stub / Minimal Job Description Safeguards**:
    - **Anti-Circular Bias Prompting**: When a job listing has only a brief title, location, or salary snippet (< 160 characters), the prompt directs the SLM to evaluate baseline viability strictly from the role title and company domain, without circular confirmation bias against the candidate's own resume.
    - **Stub Confidence & Probability Dampening**: Flags stub evaluations with `is_stub_description = True` and `confidence_level = "LOW_STUB_DESCRIPTION"`, capping interview callback probability ($\le 58\%$) to prevent inflated expectations on unverified postings.
    - The Career Cockpit displays an informative `"Role & Domain Evaluated"` tag and notice to alert the user that detailed posting text was not supplied by the source.

---

## 1. Overview & Architecture

When a candidate explores opportunities in the Career Cockpit:

1. **Unanalyzed Default State**: Newly imported and vetted opportunities display a clear **"Deep Analysis Not Yet Run"** status with a prominent **"Run Deep Analysis"** CTA. Background vetting tasks do not auto-invoke heavy LLM skill extraction, preserving cloud token quotas.
2. **Track-Bound Resume Resolution**: Resolves the specific candidate resume mapped to the job's track (e.g. `dir-plano`, `ai`, `sp-eng_plano`, `architect`).
3. **Deterministic JD Cleansing**: Uses `JDCleanserService.cleanse_heuristically(raw_desc, title=title, max_chars=10000)` to strip non-technical HR, benefits, and EEO boilerplate, reducing token load by ~30–50% prior to LLM ingestion.
4. **Dedicated Skill Radar Execution**: Executes the lightweight, end-loaded, occupation-agnostic prompt architecture:
   - **System Prompt**: Ultra-lean directive establishing pure JSON output format with zero preamble.
   - **End-Loaded Constraint Positioning**: Candidate resume and cleansed JD are placed first, with cognitive evaluation rules and the JSON template positioned immediately before the generation token, eliminating small-model (4B) attention decay and "lost-in-the-middle" prompt drift.
   - **Universal Occupation Agnosticism**: Generalizes to any profession (healthcare, accounting, finance, engineering, operations) by defining alignments structurally rather than through brittle hardcoded tech keywords.
   - `domain_alignment`: Structured assessment (`DIRECT_MATCH`, `ADJACENT_DOMAIN`, or `CROSS_INDUSTRY_MISMATCH`).
   - `seniority_alignment`: `MATCH`, `UNDERQUALIFIED`, or `OVERQUALIFIED` (with senior experience correctly recognized as `MATCH` for executive/director roles).
   - `core_competencies`: Exactly 4 hard skills or domain methodologies verified in candidate history.
   - `stretch_skills`: Exactly 2 critical requirements from the JD absent from the resume.
   - `fit_summary`: 1 concise sentence diagnosing primary transferable strength and primary gap.
5. **Dual Persistence & Instant UI Update**: Saves results immediately into `PipelineEntry` (`vetting_interview_probability`, `vetting_match_score`, `vetting_interview_reasoning`, `vetting_core_competencies`, `vetting_stretch_skills`) and caches the payload in Redis (`cockpit_llm_skill_radar_{user}_{resume}_{job}`, TTL: 7 days) for instantaneous Cockpit UI rendering.

---

## 2. Configuration & Timeouts

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `LLM_INVOKE_TIMEOUT_SECONDS` | `480` | Wall-clock deadline for LLM invocations across LiteLLM and worker thread executors. |
| `SKILL_RADAR_RESUME_MAX_CHARS` | `4000` | Maximum character slice of candidate resume provided to the model (compressed to prevent SLM latency). |
| `SKILL_RADAR_JD_MAX_CHARS` | `3500` | Maximum character slice of cleansed job description (compressed to prevent SLM latency). |
| `OLLAMA_ENABLE_THINKING` | `True` (Global) / `False` (`skill_radar`) | Global setting for reasoning models. For `skill_radar`, `think=False` is forced automatically in `factory.py` to prevent multi-minute chain-of-thought generation. |

### Local Provider Cooldown Exemption
Unlike external cloud APIs (OpenAI, Anthropic) which apply a 300-second backoff cooldown on rate limits or socket timeouts, `Ollama Local` is explicitly exempted from cooldown locks in `resume_app.llm.rate_limit`. A transient timeout on a single complex job will never lock out subsequent jobs or disable local AI across the workspace.

### Cost-Protected Cloud SLM Fallback
In `resume_app.llm.gateway`, queries of kind `USAGE_QUERY_SKILL_RADAR` enforce `max_model_tier=MODEL_TIER_SLM, prefer_local=True, allow_local=True`.
1. **Primary**: Local Ollama instance is preferred first (`prefer_local=True`).
2. **Fallback**: If Ollama Local is unreachable, times out, or is on cooldown, the gateway falls back strictly to the configured Cloud SLM fallback chain (Gemini Flash, GPT-4o-mini, Claude Haiku, Groq 8B).
3. **Frontier Model Block**: Any candidates evaluated as `MODEL_TIER_FRONTIER` (e.g. GPT-4o, Claude 3.5 Sonnet) are automatically rejected by `_ordered_eligible_candidates`, ensuring zero risk of runaway token costs during batch pipeline operations.

---

## 3. Batch Refresh Management Command

To refresh and overwrite existing diagnostic insights across all pipeline entries using local Ollama:

```powershell
# Dry run check for target user
python manage.py refresh_pipeline_skill_radar --user=joedhunt --dry-run

# Run with resume-capability: skip jobs refreshed in the last 24 hours
python manage.py refresh_pipeline_skill_radar --user=joedhunt --skip-recent

# Target specific stages (e.g. active pipeline only)
python manage.py refresh_pipeline_skill_radar --user=joedhunt --stages=vetting,applying --skip-recent

# Target a specific career track
python manage.py refresh_pipeline_skill_radar --user=joedhunt --track=dir-plano --skip-recent

# Target a limited test batch
python manage.py refresh_pipeline_skill_radar --limit=5 --skip-recent
```

### CLI Arguments

- `--user`: Username to target (default: `joedhunt`). Use `all` for all registered users.
- `--track`, `--tracks`: Specific track slug or comma-separated track slugs to target (e.g. `sp-eng_plano,architect,dir-plano`).
- `--stages`: Comma-separated stages to process (supports database stages `vetting,applying,pipeline,done` as well as Cockpit UI aliases `review,apply,new,applied`).
- `--limit`: Maximum number of entries to process (`0` for all matching).
- `--skip-recent`: Skips entries scored within the last 24 hours to enable fault-tolerant resuming.
- `--dry-run`: Inspect targeted entries and tracks without invoking Ollama.
- `--allow-cloud`: Explicit opt-in flag to permit Cloud LLM fallback. Strictly `False` by default to safeguard API token budgets during automated batch pipeline operations.

---

## 4. REST API Endpoint

The Cockpit triggers and reads Skill Radar diagnostics via:

```http
POST /jobs/cockpit/api/skill-radar/
Content-Type: application/json
X-CSRFToken: <csrf-token>

{
  "job_id": 69035,
  "track": "ic",
  "force": true
}
```

- **Mutation / Force Re-analysis (`POST`):** Requires `POST` with CSRF protection. When `force: true`, cache is bypassed, Ollama Local evaluates the listing, and vetting scores/competencies are persisted to `PipelineEntry`.
- **Read-Only Inspection (`GET`):** `GET /jobs/cockpit/api/skill-radar/?job_id=<job_id>&track=<track_slug>` provides idempotent retrieval of existing cached/persisted results without triggering background mutations.
- Response schema:
  ```json
  {
    "status": "ok",
    "job_id": 69035,
    "data": {
      "match_score": 85,
      "interview_probability": 68,
      "core_competencies": ["GCP", "Terraform", "LLM Orchestration", "API Design", "Observability"],
      "stretch_skills": ["Python", "TypeScript", "LLM Safety", "Retrieval", "Platform Engineering"],
      "fit_summary": "Strong alignment in GCP, Terraform, LLM orchestration, API design, and observability; stretch gap in Python, TypeScript, LLM safety, retrieval, and platform engineering.",
      "source": "ollama_local"
    }
  }
  ```

---

## 5. Contextual Skill Gap Severity & Deterministic Scoring Engine

### Contextual vs. Uniform Penalties
In standard ATS implementations, missing qualifications are often penalized uniformly, creating false equivalence:
- **Case 1 (Incidental Tool Gap):** An Enterprise Cloud Solutions Architect candidate possesses deep GCP, Java, microservices, and GenAI architecture, but lacks familiarity with peripheral project management or reporting tools (`Jira`, `Power BI`).
- **Case 2 (Structural Domain Gap):** The same candidate lacks core mission-critical domain systems (`Enterprise Cybersecurity`, `AI Security Test Harness`) on an AI Security Architect opening.

Treating both gaps with uniform deductions produced identical scores (e.g. 82% Match / 74% Callback Probability). To solve this, Skill Radar pairs **SLM semantic extraction** with a **deterministic Python contextual severity engine** (`evaluate_skill_gap_severity`).

### The 4 Severity Tiers

The engine evaluates each missing stretch skill in the context of the specific job's headline title and body frequency:

| Severity Tier | Match Penalty | Callback Penalty | Trigger Conditions | Example |
| :--- | :---: | :---: | :--- | :--- |
| **`CRITICAL_HEADLINE`** | `-6%` | `-8%` | Skill (or key token) appears directly in the `job_title`. | Missing *Power BI* on a *Lead Power BI Developer* role. |
| **`STRUCTURAL_REQUIREMENT`** | `-4%` | `-5%` | Skill is mentioned $\ge 3$ times in the job description body. | Missing *AI Security Test Harness* on an AI systems security role. |
| **`SECONDARY_GAP`** | `-3%` | `-4%` | Specialized domain requirement with 1–2 mentions in the JD. | Missing *Kafka Event Streaming* on a general cloud role. |
| **`INCIDENTAL_TOOL`** | `-1%` | `-1%` | Skill is a universal productivity/workflow tool (`INCIDENTAL_WORKFLOW_TOOLS`) AND not in the title AND $< 3$ mentions in the JD. | Missing *Jira* or *Power BI* on an Enterprise Cloud Architect role. |

### Universal Incidental Workflow Tools
Tools categorized in `INCIDENTAL_WORKFLOW_TOOLS` include:
`jira`, `confluence`, `trello`, `asana`, `power bi`, `tableau`, `excel`, `slack`, `visio`, `powerpoint`, `word`, `sharepoint`, `zoom`, `teams`, `miro`, `lucidchart`, `smartsheet`, `postman`, `swagger`, `git`, `github`, `gitlab`, `bitbucket`.

> [!NOTE]
> A tool in this list is **not permanently discounted**. If an incidental tool is elevated to the job's headline title (e.g., "Lead Power BI Developer"), Rule 1 supersedes Rule 2, escalating it to `CRITICAL_HEADLINE` (`-6% / -8%`).

### Core Competency Depth Adjustments
In addition to contextual gap penalties, candidate breadth and depth are dynamically adjusted:
- **Deep Core Coverage ($\ge 5$ substantiated core skills):** `+2%` Match / `+2%` Callback Probability bonus.
- **Moderate Core Coverage ($3$ substantiated core skills):** `-2%` Match / `-2%` Callback Probability deduction.
- **Minimal Core Coverage ($\le 2$ substantiated core skills):** `-5%` Match / `-6%` Callback Probability deduction.

---

### Dual-Track Scoring Architecture: Cloud LLM Primary vs. SLM Deterministic Baseline

To balance cost-effective on-prem execution with high-nuance cloud evaluations:
- **Cloud LLM (Frontier Tier):**
  - The Frontier LLM (e.g. Claude 3.5 Sonnet, GPT-4o, Gemini 1.5 Pro) evaluates holistic candidate viability, determining `match_score` and `interview_probability`.
  - In parallel, the deterministic Python rubric runs to compute `deterministic_match_score` and `deterministic_interview_probability`.
  - The LLM score serves as the primary canonical metric for cards, pipeline progression, and filtering.
  - Both scores and variance deltas (`score_delta_match`, `score_delta_interview`) are persisted in cache, API payloads, and surfaced via `#inspect-comparison-badge`.
- **Local SLM (Ollama Tier):**
  - Small models (3B-8B) exhibit calibration skew when generating raw percentages. Any model-emitted integer scores are strictly ignored.
  - The deterministic contextual rubric (`calculate_categorical_scores` + `calibrate_interview_probability`) is persisted and serves as canonical.
- **Persistence Lifecycle:**
  - `PipelineEntry.record_vetting_interview_result()` automatically persists the LLM score when Cloud LLM is evaluated, and the deterministic score when Local SLM is evaluated.

## 6. UI Presentation & Component Architecture

In the Career Cockpit inspector pane (`resume_app/career_cockpit.html`), diagnostics are rendered across five cohesive sections:

1. **Dual-Tier Model Action Bar & CTAs (Pattern B)**:
   - **Unanalyzed Placeholder CTAs**:
     - `[ 🖥️ Local SLM (Free • ~45s) ]`: Executes on-prem semantic analysis via local Ollama. 100% free with zero token expenditure.
     - `[ ⚡ Cloud LLM (~15-20s • Quota) ]`: Executes rapid analysis (~15–20s) using Cloud Frontier LLMs (e.g. OpenAI GPT-4o, Claude 3.5 Sonnet, Gemini 1.5 Pro). Triggers a Quota Warning Confirmation Modal before execution.
   - **Evaluated Inspector Header**:
     - `Source Badge` (`#radar-source-badge`): Displays `Local SLM • Verified` (emerald badge) or `Cloud LLM • Verified` (sky-blue badge), alongside `Vetting Evaluated • Local AI`.
     - `Action Buttons Group` (`#radar-action-buttons-group`): Provides dual instant re-analyze buttons: `[ SLM ]` (free re-run) and `[ Cloud LLM ]` (quota-protected re-run).
2. **Score Badges & Dual-Track Comparison Row**:
   - `Callback Probability` (`#inspect-interview-badge`): Primary callback odds, color-coded dynamically based on odds (`>= 70%` emerald, `>= 50%` amber, `< 50%` rose). Driven by Cloud LLM when frontier tier is chosen; driven by deterministic rubric on SLM.
   - `Match Score` (`#inspect-match-badge`): Primary technical match % along with verified core competency count (`85% Match (8 Verified)`).
   - `Comparative Rubric Badge` (`#inspect-comparison-badge`): When analyzed via Cloud LLM, renders a side-by-side comparative badge displaying the deterministic rubric baseline and delta (e.g., `Det. Rubric: 72% (Δ +6%)`), with a detailed hover tooltip comparing both callback probability and match score.
3. **Executive Fit Diagnosis** (`#inspect-fit-summary-box`): Emerald callout box with a clear 1-2 sentence assessment of primary strengths and critical stretch gaps.
4. **Core Competencies (Verified Skills)** (`#inspect-core-competencies`): Individual pill badges with green `check_circle` icons highlighting substantiated requirements found in the active resume.
5. **Stretch Skills (Role Gaps)** (`#inspect-stretch-skills`): Amber pill badges with `add_circle` icons denoting missing or weak requirements to be targeted during resume tailoring or interview prep.

### Client-Side State Synchronization & Timing Advisory
When running on-demand analysis via `executeDeepAnalysis(tier)`:
- **Tier-Specific Pacing**:
  - **Local SLM**: Calibrated progress display showing `0s (est. ~45s)` with progressive progress increments.
  - **Cloud LLM**: Progress display showing `0s (est. ~15-20s)` with an 18-second pacing calibration.
- **Cloud Quota Confirmation Modal** (`#deep-analysis-modal`): Clearly informs the user that proceeding will consume Cloud LLM provider quota (OpenAI, Claude, or Gemini) and requires explicit confirmation.
- **Envelope Unwrapping**: The backend `SkillRadarService._parse_llm_response` gracefully parses bare JSON, Markdown codeblocks, and nested OpenAI/assistant envelopes (e.g. `{"role": "assistant", "content": {...}}` or `{"message": {"content": ...}}`).
- **DOM Synchronization**: The client asynchronously updates the selected row's DOM dataset (`coreMatches`, `stretchSkills`, `matchScore`, `interviewProbability`, `fitSummary`, `radarSource`) before re-rendering the inspector with `selectJob(row)`, ensuring verified competency pills and stretch gap badges render immediately upon completion.

---

## 7. Persistence Lifecycle & Refresh Guarantees

To ensure diagnostics are never lost across browser refreshes, cache flushes, or server restarts:

1. **Transactional Database Write**:
   - `cockpit_skill_radar_api` immediately updates all active `PipelineEntry` instances for the user and target job listing with `vetting_match_score`, `vetting_interview_probability`, `vetting_interview_reasoning`, `vetting_core_competencies`, and `vetting_stretch_skills`.
   - If no `PipelineEntry` exists yet (e.g. evaluating a newly found job), it automatically creates one in the active track to persist the evaluation permanently.
2. **Deterministic Resume Binding**:
   - Both the API endpoint and the view render invoke `_resolve_cockpit_bound_resume(user, track_slug)` to ensure matching cache keys and resume associations regardless of track switches or reloads.
3. **Database Rehydration on Cold Cache Miss**:
   - In `pipeline_board_view`, if Redis or memory cache is cold or missed, `has_db_analysis` checks for substantiated diagnostics (`vetting_match_score`, `vetting_interview_reasoning`, `zero_llm_core_matches`, `zero_llm_stretch_skills`).
   - If present in the database, `pipeline_board_view` rehydrates `zero_llm_match_pct`, `zero_llm_core_json`, `zero_llm_stretch_json`, `zero_llm_fit_summary`, and sets `skill_radar_source="local_slm"`.
   - Even if `core_competencies` is empty (e.g., cross-industry domain divergence with 0 matching core skills and 3 stretch gaps), the role is preserved as evaluated with `Local SLM • Deep Analysis` rather than reverting to `Unanalyzed`.
4. **Middle Feed Card Pill Rendering**:
   - The job card's match badge dynamically displays emerald status with `Verified` count when core competencies match, `Gaps` count when only stretch skills exist, or default score if unanalyzed.

---

## 8. Batch Pipeline Skill Radar Refresh Automation

For comprehensive pipeline re-evaluation following model updates, prompt tuning, or grounding logic adjustments, a dedicated management command and multi-tenant batch runner are provided:

### Management Command: `refresh_pipeline_skill_radar`
```bash
python manage.py refresh_pipeline_skill_radar \
    --user <username|all> \
    --stages <vetting,applying,pipeline,done> \
    [--track <slug>] \
    [--limit <n>] \
    [--skip-recent] \
    [--dry-run]
```

- **Execution Engine:** Local SLM via Ollama (`hf.co/empero-ai/Qwen3.8-4B-Distill-GGUF:Q4_K_M`) by default, preserving cloud privacy and zero API token costs.
- **Cache & Database Synchronization:**
  - Persists evaluated match score, interview callback probability, reasoning, core competencies, and stretch gaps to `PipelineEntry`.
  - Invalidate and warms the Redis key (`cockpit_ollama_skill_radar_{user_id}_{resume_id}_{job_id}`) with standard 7-day TTL.
- **In-Memory PDF Caching:** Reuses in-memory parsed resume text across multiple pipeline jobs for the same user track, maximizing throughput.

### Batch Runner Script: `scripts/run_skill_radar_pipeline_refresh.py`
A convenience script allowing multi-tenant orchestration with real-time ETA telemetry and failure isolation:
```bash
python scripts/run_skill_radar_pipeline_refresh.py --users joedhunt,joeblue --stages vetting,applying
```
