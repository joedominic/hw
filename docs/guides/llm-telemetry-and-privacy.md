# LLM Telemetry, Tracing & Selective PII Redaction

This document specifies the Langfuse observability integration, trace and generation naming schema, and selective PII redaction policies used across HireEdge's LLM pipeline.

---

## 1. Overview & Problem Solved

HireEdge integrates [Langfuse](https://cloud.langfuse.com) via LiteLLM's telemetry hooks to trace all LLM invocations across the career operating system (fit check, resume optimizer, ATS/recruiter evaluations, job matching, authenticity checking, and skill extraction).

Previously, two issues degraded monitoring usability:
1. **Total Content Redaction:** Setting redaction enabled wiped out the entire input prompt and output response, replacing them with `"redacted-by-litellm"`. This prevented developers and support staff from inspecting agent reasoning, prompt effectiveness, or model hallucinations.
2. **Generic Trace & Observation Names:** Every call defaulted to `Name: litellm-completion` and `Trace Name: litellm-completion`. This made it impossible to filter, group, or analyze calls by workflow or feature in the Langfuse dashboard.

---

## 2. Trace & Generation Naming Architecture

To enable grouping and granular analysis in Langfuse, the LLM gateway and `LiteLLMChatModel` resolve semantic identifiers for every call:

- **Trace Name (`trace.name`):** Identifies the overall workflow, feature, or lifecycle context (e.g., `resume_optimizer`, `fit_check`, `job_matching`, `pipeline_skill_extract`). Multi-step workflows (like resume tailoring) group under the same trace name.
- **Generation Name (`generation.name` / `name`):** Identifies the specific step or node within that workflow (e.g., `optimizer_writer`, `optimizer_ats_judge`, `optimizer_recruiter_judge`, `jd_cleanse`).

### Standard Mapping (`resume_app.llm.telemetry.TRACE_NAME_MAPPINGS`)

| Usage Query Kind | Trace Name (`trace.name`) | Generation Name (`generation.name`) | Workflow Type |
|---|---|---|---|
| `optimizer_writer` | `resume_optimizer` | `optimizer_writer` | Multi-step Workflow |
| `optimizer_ats_judge` | `resume_optimizer` | `optimizer_ats_judge` | Multi-step Workflow |
| `optimizer_recruiter_judge` | `resume_optimizer` | `optimizer_recruiter_judge` | Multi-step Workflow |
| `jd_cleanse` | `resume_optimizer` (or `jd_cleanse`) | `jd_cleanse` | Pre-processing |
| `fit_check` | `fit_check` | `fit_check` | Standalone Evaluation |
| `matching` | `job_matching` | `job_matching` | Batch / Funnel |
| `job_insights` | `job_insights` | `job_insights` | Insights Generation |
| `cover_letter` | `cover_letter` | `cover_letter` | Asset Generation |
| `interview_prep` | `interview_prep` | `interview_prep` | Asset Generation |
| `jobs_ai_match` | `jobs_ai_match` | `jobs_ai_match` | Search Matching |
| `keyword_search_fit` | `keyword_search_fit` | `keyword_search_fit` | Lexical Fit |
| `jobs_match_api` | `jobs_match_api` | `jobs_match_api` | API REST |
| `pipeline_vetting_matching` | `pipeline_vetting` | `pipeline_vetting_matching` | Scraper Intake |
| `pipeline_resume_refine` | `pipeline_refine` | `pipeline_resume_refine` | Resume Refinement |
| `pipeline_skill_extract` | `pipeline_skill_extract` | `pipeline_skill_extract` | Background Ingestion |
| `job_authenticity` | `job_authenticity` | `job_authenticity` | Fraud Guardrail |
| `skill_radar` | `skill_radar` | `skill_radar` | Skill Taxonomy |
| `career_profiler` | `career_profiler` | `career_profiler` | Onboarding Profiler |
| `prompt_studio` | `prompt_studio` | `prompt_studio_eval` | Test Sandbox |
| `api_llm_complete` | `api_completion` | `api_llm_complete` | Commercial API |
| `api_resume_fit` | `api_resume_fit` | `api_resume_fit` | Commercial API |

### Session & Tenant Attribution
- **`session_id`:** Set to `job_cache_key` or workflow ID, linking all steps of a resume optimization or evaluation together.
- **`user_id` / `trace_user_id`:** Set to the authenticated user ID for multi-tenant usage filtering.

---

## 3. Selective PII Redaction (`resume_app.llm.pii`)

Instead of replacing prompt content with placeholder strings, HireEdge implements recursive, deterministic **Selective PII Redaction** via `mask_pii_data` and `mask_pii_text`.

### Scrubbed Entities & Placeholders

1. **Candidate & User Names (`[NAME]`):**
   - Labeled patterns: `Candidate Name: [NAME]`, `Full Name: [NAME]`, `First Name: [NAME]`, `Last Name: [NAME]`
   - Resume top headers: `# John Doe` preceding contact coordinates becomes `# [NAME]`
   - Salutations & sign-offs: `Dear [NAME],`, `Sincerely,\n[NAME]`
   - Embedded JSON fields: `{"name": "[NAME]", "candidate_name": "[NAME]"}`
   - Dictionary keys: `name`, `full_name`, `candidate_name`, `first_name`, `last_name`, `applicant_name`, `contact_name`
2. **Street & Postal Addresses (`[ADDRESS]` / `[ZIP]`):**
   - Street addresses (`\b\d{1,6}\s+...\bStreet\b|\bSt\b|\bAvenue\b...`)
   - PO Boxes (`P.O. Box 1234`)
   - City, State ZIP strings (`Seattle, WA 98101`)
   - Labeled address fields (`Address: [ADDRESS]`)
   - Dictionary keys: `address`, `street_address`, `home_address`, `postal_address`
3. **Email Addresses (`[EMAIL]`):**
   - Full standard RFC email patterns (`user@domain.com`)
   - Dictionary keys: `email`, `candidate_email`, `contact_email`
4. **Phone Numbers (`[PHONE]`):**
   - Standard US/international phone formats (`(555) 123-4567`, `+1-555-123-4567`, `555.123.4567`)
   - Labeled phone lines (`Phone: [PHONE]`, `Mobile: [PHONE]`)
   - Dictionary keys: `phone`, `phone_number`, `mobile`, `telephone`, `cell_phone`
5. **Government IDs (`[SSN]`):**
   - Social Security Numbers (`\b\d{3}-\d{2}-\d{4}\b`)
   - Dictionary keys: `ssn`, `social_security_number`
6. **Social & Portfolio Handles (`[LINKEDIN]`, `[GITHUB]`):**
   - Profile URLs (`https://linkedin.com/in/[handle]`, `https://github.com/[handle]`)

### Content Preservation
Non-PII context is 100% preserved in telemetry:
- Job descriptions and requirements
- Skills, qualifications, and domain experience
- ATS match scores and scoring criteria
- Tailored resume bullet points and suggestions
- Agent reasoning, prompts, and system instructions

---

## 4. Configuration & Environment Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `LANGFUSE_PUBLIC_KEY` | string | `""` | Public API key from Langfuse Cloud project settings |
| `LANGFUSE_SECRET_KEY` | string | `""` | Secret API key from Langfuse Cloud project settings |
| `LANGFUSE_BASE_URL` | string | `https://cloud.langfuse.com` | Langfuse host URL (e.g., `https://us.cloud.langfuse.com`) |
| `LANGFUSE_ENABLED` | boolean | Auto | Enables telemetry export (defaults to `True` when keys are set) |
| `LANGFUSE_MASK_PII` | boolean | `True` | **Primary privacy guardrail.** Scrubs PII (name, address, email, phone) before telemetry export while preserving full prompt/response transparency. |
| `LANGFUSE_REDACT_CONTENT` | boolean | `False` | **Emergency zero-content mode.** When `True`, completely replaces prompt and output text with `"redacted-by-litellm"`. Use only for extreme compliance requirements. |
| `LANGFUSE_DROP_ALL_CONTENT` | boolean | `False` | Alias for emergency zero-content mode. |
| `OLLAMA_ENABLE_THINKING` | boolean | `True` | When `True` (default), allows Ollama models with reasoning capabilities (e.g., Nemotron, DeepSeek-R1) to utilize their full chain-of-thought scratchpad for accurate evaluation. When `False`, forces `think=False` for maximum speed. |
| `LLM_INVOKE_TIMEOUT_SECONDS` | integer | `300` | Wall-clock deadline for LLM invocations across LiteLLM and HTTP transports (default increased to 300s to support local thinking models on long inputs). |

---

## 5. Ollama Reasoning / Think Mode Guardrails

Certain local Ollama models (such as `hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M` or `deepseek-r1`) include reasoning/thinking capabilities. By default, Ollama's Jinja template pre-populates generation prompts with `<|im_start|>assistant\n<think>\n`, enabling the model to generate an internal chain-of-thought scratchpad before answering.

### System Safeguards:
1. **Residual Tag Sanitization & Content Separation:** If an Ollama model emits a `</think>` delimiter in its response, `LiteLLMChatModel` automatically splits the content, sets `response.content` to the actual answer, and routes the extracted reasoning monologue to `response.additional_kwargs["reasoning_content"]`. This ensures downstream JSON parsers and UI text never break or display raw reasoning monologues.
2. **Speed vs. Accuracy Toggle (`OLLAMA_ENABLE_THINKING`):**
   - **`True` (Default):** Thinking is enabled. Compact reasoning models (like 4B Nemotron) retain their full chain-of-thought scratchpad, ensuring high evaluation accuracy, constraint-checking, and structured compliance.
   - **`False`:** Forces `think=False` on Ollama calls, which bypasses the thinking phase for near-instant (sub-second) responses, at the expense of zero-shot reasoning depth.
3. **Payload & Prompt Optimization for Local Reasoning Models:**
   - **Heuristic JD Cleansing:** Automatically runs `JDCleanserService.cleanse_heuristically()` on job postings to strip non-technical boilerplate (EEO, benefits, culture) prior to prompt construction, eliminating ~350-500 tokens of prompt noise.
   - **XML Boundary Delimitation:** Encapsulates `<CANDIDATE_RESUME>` and `<TARGET_JOB>` in explicit tags to avoid attentional bleed and reduce internal cross-document searching.
   - **Reasoning Scaffold:** The system prompt enforces a bounded 3-point scratchpad (`Matched: [...]`, `Gaps: [...]`, `Score: [...]`), preventing models from generating unconstrained 3,000+ token monologues and reducing latency by >65%.

---

## 6. Verification & Testing

Automated test suites verify selective PII masking, telemetry naming, and Ollama thinking safeguards without requiring external network access:

```powershell
python django_project/manage.py test resume_app.test_langfuse_telemetry --noinput
```

Coverage includes:
- Text and dictionary PII scrubbing without data leaks
- Non-PII content retention (ATS metrics, job titles, technical skills)
- Multi-step trace grouping (`resume_optimizer` parent with individual step names)
- Graceful fallbacks for direct `LiteLLMChatModel` invocations
- Default deactivation of Ollama `think` mode
- Sanitization and extraction of residual `</think>` reasoning blocks
