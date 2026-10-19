# Skill Radar & Fit Diagnostics

The **Skill Radar & Fit Diagnostics** subsystem extracts deep technical competencies, stretch skill gaps, realistic interview callback probabilities, and an executive rationale comparing a candidate's track-bound resume to a target job description in the Career Cockpit.

---

## 1. Overview & Architecture

When a job is inspected in the Career Cockpit or scored during pipeline vetting, the system runs the `skill_radar` analysis pipeline:

1. **Track-Bound Resume Resolution**: Locates the specific candidate resume mapped to the job's track (e.g. `dir-plano`, `ai`, `sp-eng_plano`, `architect`).
2. **Deterministic JD Cleansing**: Uses `JDCleanserService.cleanse_heuristically(raw_desc, title=title, max_chars=10000)` to strip non-technical HR, benefits, and EEO boilerplate, reducing token load by ~30–50% prior to LLM ingestion.
3. **Prompt Cache Optimization**: Orders `<CANDIDATE_RESUME>` ahead of `<TARGET_JOB>` in the prompt to maximize KV cache reuse across multiple jobs scored against the same candidate resume.
4. **Local Ollama Reasoning Execution**: Calls `hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M` via LiteLLM over local LAN (`http://192.168.2.6:11434`) with a strict `<think>` scratchpad constraint under 100 words.
5. **Interview Probability Calibration**: Calibrates the raw match score against role seniority tiers (Executive, Staff/Principal, Senior, Junior) and gap ratios to produce a realistic interview screening probability.
6. **Dual Persistence**: Saves results immediately into `PipelineEntry` (`vetting_interview_probability`, `vetting_match_score`, `vetting_interview_reasoning`, `vetting_core_competencies`, `vetting_stretch_skills`) and caches the payload in Redis (`cockpit_ollama_skill_radar_{user}_{resume}_{job}`) for instantaneous Cockpit UI rendering.

---

## 2. Configuration & Timeouts

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `LLM_INVOKE_TIMEOUT_SECONDS` | `480` | Wall-clock deadline for LLM invocations across LiteLLM and worker thread executors. |
| `SKILL_RADAR_RESUME_MAX_CHARS` | `12000` | Maximum character slice of candidate resume provided to the model. |
| `SKILL_RADAR_JD_MAX_CHARS` | `10000` | Maximum character slice of cleansed job description. |
| `OLLAMA_ENABLE_THINKING` | `True` | Enables thinking/reasoning traces for reasoning models (e.g. Nemotron, DeepSeek-R1). |

### Local Provider Cooldown Exemption
Unlike external cloud APIs (OpenAI, Anthropic) which apply a 300-second backoff cooldown on rate limits or socket timeouts, `Ollama Local` is explicitly exempted from cooldown locks in `resume_app.llm.rate_limit`. A transient timeout on a single complex job will never lock out subsequent jobs or disable local AI across the workspace.

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
- `--stages`: Comma-separated stages to process (default: `vetting,applying,pipeline,done`).
- `--track`: Specific track slug to target (e.g. `dir-plano`, `ai`, `architect`, `sp-eng_plano`).
- `--limit`: Maximum number of entries to process (`0` for all matching).
- `--skip-recent`: Skips entries scored within the last 24 hours to enable fault-tolerant resuming.
- `--dry-run`: Inspect targeted entries and tracks without invoking Ollama.

---

## 4. REST API Endpoint

The Cockpit triggers and reads Skill Radar diagnostics via:

```http
GET /jobs/cockpit/api/skill-radar/?job_id=<job_id>&track=<track_slug>&force=<1|0>
```

- When `force=1`, cache is bypassed and Ollama Local re-analyzes the job.
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

## 5. UI Presentation & Component Architecture

In the Career Cockpit inspector pane (`resume_app/career_cockpit.html`), diagnostics are rendered across four cohesive sections:

1. **Score Badges Row**:
   - `Callback Probability` (`#inspect-interview-badge`): Color-coded dynamically based on odds (`>= 70%` emerald, `>= 50%` amber, `< 50%` rose).
   - `Match Score` (`#inspect-match-badge`): Technical match % along with verified core competency count (`85% Match (8 Verified)`).
2. **Executive Fit Diagnosis** (`#inspect-fit-summary-box`): Emerald callout box with a clear 1-2 sentence assessment of primary strengths and critical stretch gaps.
3. **Core Competencies (Verified Skills)** (`#inspect-core-competencies`): Individual pill badges with green `check_circle` icons highlighting substantiated requirements found in the active resume.
4. **Stretch Skills (Role Gaps)** (`#inspect-stretch-skills`): Amber pill badges with `add_circle` icons denoting missing or weak requirements to be targeted during resume tailoring or interview prep.

### Client-Side State Synchronization
When running on-demand analysis via the `Re-Analyze` button (`executeDeepAnalysis`), the client asynchronously updates the selected row's DOM dataset (`coreMatches`, `stretchSkills`, `matchScore`, `interviewProbability`, `fitSummary`, `radarSource`) before re-rendering the inspector with `selectJob(row)` to ensure zero desync between the API payload and the displayed badges.
