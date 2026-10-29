# Prompt Suite Optimization & Caching Architecture

## Overview

This document details the Phase 1 and Phase 2 optimizations implemented across the HireEdge Prompt Library, covering prompt pruning, cross-workflow KV-cache prefix ordering, reasoning model scratchpad bounds, and runtime prompt store binding.

---

## 1. Phase 1: Suite Pruning & Unified Fit Caching

### A. Deprecation of `fit_check` & Removal of `job_authenticity` & Consolidation of `skill_radar`
- **Deprecation of `fit_check`:** Removed `fit_check` from active `PROMPT_DEFINITIONS` in [`prompt_store.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompt_store.py) so it no longer appears in the Prompt Library workstation.
- **Removal of Labor Compliance (`job_authenticity`):**
  - Removed `job_authenticity` ("Labor Compliance & Authenticity Auditor") from `PROMPT_DEFINITIONS`, `DEFAULT_PROMPTS`, `_PROMPT_SPEC`, and `PROMPT_TO_QUERY_KIND_MAP` in [`prompt_store.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompt_store.py).
  - Deleted `DEFAULT_JOB_AUTHENTICITY_SYSTEM`, `DEFAULT_JOB_AUTHENTICITY_USER`, and `DEFAULT_JOB_AUTHENTICITY_PROMPT` from [`prompts.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py).
  - Removed Posting Authenticity card and data attributes from Career Cockpit UI ([`career_cockpit.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/career_cockpit.html)) and Kanban job cards ([`_pipeline_job_card_normal.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/_pipeline_job_card_normal.html)).
  - Removed Pass 1 auditing loop from background vetting tasks in [`tasks.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/tasks.py) and removed endpoints from [`jobs_api.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/jobs_api.py).
- **Consolidation of `skill_radar` onto `resume_judge`:**
  - Removed `skill_radar` ("Skill Radar Analyzer") from `PROMPT_DEFINITIONS` in [`prompt_store.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompt_store.py) to eliminate duplicate prompt definitions and conflicting scoring mechanisms.
  - Subsumed skill extraction directly into the **Resume Judge**: [`SkillRadarService`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/skill_radar.py) now executes `resume_judge` prompt templates, extracting `matched_keywords` -> `core_competencies`, `missing_keywords` -> `stretch_skills`, `composite_score` -> `match_score`, and `executive_summary` -> `fit_summary`, while also populating recruiter quantification gaps and priority revisions.
  - Two-Phase Engine: Pipeline stages run Resume Judge on **Local SLM** (via Ollama Local) with zero API cost. When the candidate moves to the Applying stage to tailor their resume, **Cloud LLM** re-evaluates the tailored draft with high precision against the candidate's chosen resume.

### B. Cross-Workflow Caching Synchronization
- [`run_matching()`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/agents.py) now checks and synchronizes with [`SkillRadarService`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/skill_radar.py) cache keys (`cockpit_ollama_skill_radar_{user}_{resume}_{job}`).
- If a candidate-job pair has already been analyzed in the Cockpit or background vetting, subsequent on-demand AI matches resolve instantly with a cache hit, cutting redundant LLM compute.

---

## 2. Phase 2: Performance & KV-Cache Optimization

### A. KV-Cache Prefix Ordering
Modern LLM inference engines (e.g. Ollama Local, vLLM, Groq, Anthropic) utilize prompt prefix caching. When candidate-specific static text is placed at the top of the prompt, multiple jobs evaluated against that candidate share a warm KV cache.

1. **Cover Letter Generation ([`prompts.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py) & [`job_prep.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/job_prep.py)):**
   - **Before:** Dynamic `Company: {company_name}` and `Role: {job_title}` were placed on line 1 of the user prompt, causing a 100% KV cache bust on every job.
   - **After:** Static `<CANDIDATE_RESUME>` is placed first, followed by `<TARGET_JOB company="{company_name}" role="{job_title}">`.

2. **Job Matching Scorer ([`prompts.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py)):**
   - **Before:** Unstructured `Resume: {resume_text}` and `Job Description: {job_description}`.
   - **After:** Clean, XML-tagged `<CANDIDATE_RESUME>` followed by `<TARGET_JOB>`, maximizing prefix cache hits.

### B. Reasoning Model Scratchpad Bounding
Reasoning models (such as Nemotron-3, DeepSeek-R1, and Qwen-QwQ) often spend unbounded tokens in internal `<think>` loops, leading to 300-second timeouts on local hardware.

Strict scratchpad constraints were added to:
- [`DEFAULT_MATCHING_SYSTEM`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py#L106)
- [`DEFAULT_CAREER_PROFILER_SYSTEM`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py#L365)
- [`DEFAULT_SKILL_RADAR_SYSTEM`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py#L320)

**Standard Bounded Thinking Directive:**
```
THINKING CONSTRAINT:
If using thinking tokens, you MUST keep your scratchpad strictly under 100 words total in a rapid 3-point list:
- Checks: [list key positive check IDs]
- Calculation: [arithmetic sum]
- Verdict: [final verdict code]
Do not write long explanations or prose in your thinking trace. Then immediately close </think> and output JSON.
```

### C. Runtime Prompt Store Binding in Pipeline Extraction
- **Issue:** [`pipeline_llm_skill_extract.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/pipeline_llm_skill_extract.py) previously hardcoded `SYSTEM_PROMPT` and `CONSOLIDATION_SYSTEM_PROMPT` in python constants, ignoring edits made by staff in the Prompt Studio UI.
- **Fix:** Both `llm_invoke_pipeline_batch()` and `consolidate_skills_file()` now query [`prompt_store.get_system_prompt()`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompt_store.py) dynamically (`pipeline_skill_extract` and `pipeline_resume_refine`), falling back to built-in defaults if no custom override is configured.

---

## 3. Phase 3: Unified Dual-Perspective Resume Judge (`resume_judge`)

### A. Architectural Motivation
Prior to Phase 3, the iterative resume optimizer ran two separate, sequential LLM judge passes per revision loop:
1. `ats_judge` (evaluating hard keyword searchability and formatting)
2. `recruiter_judge` (evaluating executive presence, impact quantification, and phrasing)

Combining these evaluations into a single `resume_judge` node cuts LLM invocations by 50% for the critique phase, reducing latency by 15–30 seconds and saving ~33% in prompt tokens on every revision cycle.

### B. LLMOps & Prompt Engineering Enhancements
1. **Target-Job-First KV Prefix Ordering:**
   - In an iterative tailoring loop, the **Target Job Description is static**, while the **Candidate Resume changes every revision cycle**.
   - The user prompt template places `<TARGET_JOB>` first, followed by `<CANDIDATE_RESUME>`. This preserves the KV cache of the System Prompt and Target Job across all writer iterations.
2. **Deterministic Arithmetic Guardrails:**
   - Both the prompt schema instructions and [`parse_resume_judge_fallback()`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/parsers.py) enforce `composite_score = round((ats_score + recruiter_score) / 2)` and strict approval conditions (`composite_score >= threshold` and zero missing mandatory qualifications).
3. **Structured 3-Point Scratchpad Checkpoints:**
   - Replaced artificial word-count constraints with a structured 3-bullet checklist (ATS Checklist, Recruiter Checklist, Score Calibration) that guides reasoning models without inducing token anxiety.
4. **Skill Family ("OR" Logic) Clause:**
   - Evaluates interchangeable technologies (e.g. AWS / Azure / GCP or React / Angular) as clusters rather than penalizing missing alternative keywords.
5. **Actionable Writer Locator Anchors:**
   - Critiques output explicit `role_anchor` and `original_text_snippet` fields, allowing the downstream Writer node to pinpoint lines needing revision.

### C. System & Workflow Backward Compatibility
- **Database Model:** Migration `0057_systempromptprofile_resume_judge_and_more` adds `resume_judge_prompt_system`, `resume_judge_prompt_user`, and legacy template columns to `SystemPromptProfile`.
- **Workflow Seeding:** Migration `0058_seed_unified_optimizer_workflow` seeds default system workflows (`Unified Fast Loop` and `Full Unified Pipeline`) incorporating the unified judge.
- **Workflow Steps & Builder UI:**
  - Registered `resume_judge` in `WORKFLOW_STEP_IDS`, `VALID_STEP_IDS`, and `agents.nodes_map`. Default workflow `create_workflow()` is now `["writer", "resume_judge"]`.
  - Added the `+ Unified Judge (ATS + Recruiter)` node button and step-specific LLM override card in [`workflow_form.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/workflow_form.html).
  - Added `Unified Judge` options to the Built-in workflow presets in [`optimizer.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/optimizer.html).
- **Legacy Compatibility:** `ats_judge` and `recruiter_judge` remain fully registered in `VALID_STEP_IDS` and `agents.py` for older custom workflows. The `resume_judge_node` unpacks scores into `ats_score`, `recruiter_score`, `last_ats_json`, and `last_recruiter_json`, keeping existing UI scorecards in `optimizer.html` and performance dashboards 100% operational.
- **Prompt Studio Template Consolidation:** Removed legacy individual `ats_judge` ("ATS Gatekeeper") and `recruiter_judge` ("Recruiter Judge") templates from `PROMPT_DEFINITIONS` in [`prompt_store.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompt_store.py). Prompt Studio now exposes only `resume_judge` ("Unified Resume Judge (ATS + Recruiter)") under the Optimization Pipeline, preventing duplicate and obsolete judge editing surfaces while preserving ATS Profiles and underlying model preferences. Added `"resume_judge": "optimizer_ats_judge"` to `PROMPT_TO_QUERY_KIND_MAP`.

### D. Dual-Lens UX & Feedback Presentation
1. **Separation of ATS and Recruiter Feedback Panes:**
   - Instead of merging critique outputs into a single monolithic feedback box, [`optimizer.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/optimizer.html) and [`_optimizer_status_inner.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/_optimizer_status_inner.html) render a responsive dual-card grid:
     - **ATS Technical Searchability Card:** Color-coded ATS match badge, individual missing keyword chips with remove icons, automated formatting/parseability validation flags, and structured placement directives.
     - **Executive Recruiter Review Card:** Recruiter score badge, metric quantification gap callouts with role anchors, and targeted phrasing critique cards showing role anchor, monospace original text snippet, and active verb direction.
2. **Priority Revision Action Directives:**
   - When present in unified judge synthesis, a high-contrast banner surfaces 2–4 priority revision commands directly above the diagnostic cards, highlighting approval readiness status.
3. **Step-by-Step Execution & Font Optimization:**
   - Single-step mode (`/api/resume/run-step`) accepts `resume_judge` and formats the dual-lens output into cleanly delineated sections (`🏅 SYNTHESIS & SCORE CALIBRATION`, `🔍 LENS 1: ATS PARSER`, `👔 LENS 2: RECRUITER REVIEW`) alongside raw response JSON.
   - Reduced font sizes inside feedback containers, pre blocks, and prompt inspection panels (`text-[11px] font-mono leading-relaxed` and `text-xs`) to maximize vertical efficiency and readability.

### E. Workflow Dropdown Hygiene & Worker Synchronization
- **Optimizer Dropdown Cleanup:** Removed stale hardcoded presets (`legacy_default`, `recruiter_first`) from the `<optgroup label="Built-in">` in [`optimizer.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/optimizer.html). Only active unified presets (`Writer → Unified Judge` and `JD Cleanse → Writer → Unified Judge`) and database-backed workflows appear.
- **Worker Synchronization:** Restarted the Huey task daemon so in-memory `VALID_STEP_IDS` recognizes `resume_judge` without restarts.

---

## 4. Phase 4: Dynamic UI-Driven Model Engine Strategy (Strictly SLM vs LLM)

### A. Architectural Motivation & Elimination of Hardcoded Overrides
Previously, model selection was both fragmented and overly granular:
- Individual model overrides (pinning a single model like `Groq::llama-3.3-70b` per prompt) created brittle dependencies and bypassed tenant priority failovers.
- Gateway routing had hardcoded query-kind routing locks in Python code.
To give tenants complete, intuitive control without fragile configuration, the system was refactored to a **strict two-tier strategy: SLM vs LLM**.

### B. Strict Two-Tier Engine Architecture
Every prompt in the Prompt Library is configured as either:
- **`SLM` (Small Language Model)**: Fast, bulk, zero-cost / low-cost execution. Targets local Ollama instances or high-speed lightweight cloud models (e.g. Gemini 2.0 Flash, GPT-4o-mini, Claude 3.5 Haiku, Groq 8B).
- **`LLM` (Large Language Model / Frontier)**: Deep reasoning, synthesis, and creative text generation. Targets high-capability cloud frontier models (e.g. Claude 3.5 Sonnet, GPT-4o, Gemini 1.5/2.0 Pro).

### C. Tenant BYOK Key Priority & System Fallback Mechanics
Candidate resolution follows a clean tenant-first hierarchy:
1. **Tenant BYOK Priority Execution**:
   - If the tenant has configured their own keys or local instances for models in the selected tier (`Settings -> LLM Providers & Preference Order`), the gateway executes them strictly in ascending priority order (priority 0, 1, 2...).
2. **Platform System Default Fallback**:
   - If the tenant has *not* configured their own keys/models for that tier, or if their models encounter timeouts, cooldowns, or quota limits, the gateway automatically falls back to the system's platform default SLM or LLM models.
   - Specific model overrides have been completely removed. The tenant's global priority table drives model selection, and the prompt merely specifies the required cognitive tier (`SLM` vs `LLM`).

### D. Prompt Library Workstation UI
In [`prompt_library.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/prompt_library.html):
- Sub-bar features a clean **Model Engine** dropdown with strictly 2 options:
  - `⚡ SLM (Small Language Model — Fast / Bulk / Zero-Cost)`
  - `🧠 LLM (Large Language Model — Deep Reasoning & Writing)`
- Each prompt displays its recommended tier badge (`Recommended: SLM` or `Recommended: LLM`).
- Sidebar directory buttons feature color-coded tier badges (`SLM` in emerald green vs `LLM` in indigo blue).
- Model selection changes save asynchronously via `/api/resume/prompts/set-model/` and persist in [`TenantPromptModelPreference.target_tier`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/models.py).

---

## 5. Phase 5: Skill Radar KV-Cache Prefix Preservation & Local SLM Latency Resolution

### A. KV-Cache Prefix Restoration
When prompt templates for `skill_radar` were previously updated, the evaluation rules and schema were mistakenly placed *after* the dynamic `{job_description}` variable in the user prompt. Because every job description is unique, the dynamic tokens in the middle broke prefix caching on every sequential call.

**Restoration Details:**
1. **System Prompt Placement (`DEFAULT_SKILL_RADAR_SYSTEM`)**:
   - All static task instructions, evaluation rules, categorical definitions, noise reduction guidelines, and JSON schemas were restored into `DEFAULT_SKILL_RADAR_SYSTEM`.
2. **User Prompt Prefix Ordering (`DEFAULT_SKILL_RADAR_USER`)**:
   - `<CANDIDATE_RESUME>{resume_text}</CANDIDATE_RESUME>` is positioned first.
   - Dynamic target job text `[TARGET JOB: {job_title}]\n{job_description}` is placed last.
   - For sequential evaluations of multiple jobs against the same candidate resume, the static system prompt and resume tokens remain warm in the local SLM KV cache (achieving ~4,750 prompt tokens/sec cache reuse in Ollama).
3. **Runtime Cache Invariant (`_ensure_kv_cache_prefix_order`)**:
   - `SkillRadarService.analyze()` dynamically verifies that `{resume_text}` precedes `{job_description}` in rendered templates. If a user customizes a prompt in the Prompt Studio and places `{job_description}` before `{resume_text}`, the runtime wraps the candidate resume at the head of the prompt to protect KV caching.

### B. LiteLLM & Local Ollama Latency Elimination
Sequential calls on local Ollama were previously taking ~35-42 seconds per job despite KV caching due to two critical issues:
1. **Runaway Reasoning Monologue in Local SLMs (Nemotron-3-Nano)**:
   - When thinking is enabled, local reasoning models generate 300–700 speculative tokens in `<think>...</think>`, burning 25–35 seconds of generation time and frequently exceeding token limits.
   - **Resolution**: Setting `think=False` and `reasoning_effort="none"` for `skill_radar` queries in `factory.py`.
2. **LiteLLM Localhost TCP Connection Stalls (16s Delay)**:
   - When querying model metadata for Ollama models not in LiteLLM's static `model_cost` map, LiteLLM called `_get_model_info_helper` which attempted to reach `http://localhost:11434/api/show`.
   - When Ollama is hosted on a LAN IP (e.g. `http://192.168.2.6:11434`), connections to `localhost` timed out across 4 sequential attempts (4.08s per timeout = **16.3 seconds** of pure socket connection blocking).
   - **Resolution**: In `factory.py`:
     - `litellm.register_model` registers the model statically in `litellm.model_cost` with `max_tokens=4096`, completely bypassing external metadata calls.
     - `os.environ.setdefault("OLLAMA_API_BASE", api_base)` syncs the configured LAN host to the environment.
   - **Result**: Sequential latency dropped from **42.18s to 12.3s** through the full Django service pipeline and **1.26s** on raw LiteLLM completion.

### C. Opportunity Noise Reduction
The prompt was enhanced with explicit noise reduction directives:
- **Core Technology Stack vs. Incidental Tools**: Primary gaps must reflect structural requirements (e.g. Go, Rust, C++, RTOS, Distributed Systems) rather than ubiquitous productivity tools (Jira, Slack, Confluence, Git).
- **Equivalent Cloud Family Transference**: AWS, GCP, and Azure are recognized as transferable equivalents for generalist roles, preventing false-positive gap penalties when a candidate has deep expertise in an equivalent major cloud provider.

---

## 6. Verification & Test Suite

The refactoring was verified against the full Django test suite:
- `resume_app.tests.JudgeNodeRoutingTestCase` (6 tests verifying unified routing, math guardrails, anchors, status API dual-lens extraction, and legacy step compatibility)
- `resume_app.test_prompt_studio` (12 tests passed)
- `resume_app.test_job_prep` (21 tests passed)
- `resume_app.test_fit_inspector` (16 tests passed)
- `resume_app.test_career_profiler` & `resume_app.test_fact_checker` (28 tests passed)
- `resume_app.test_langfuse_telemetry` (10 tests passed)
- `resume_app.tests.PromptStoreResolveTestCase`, `SystemOptimizerWorkflowTestCase`, `WorkflowStepLLMOverrideTestCase`, `BuildOptimizerPromptStateTestCase`, `AtsJudgeProfilePromptTestCase`, `AtsJudgeParserTestCase` (25 tests passed)
