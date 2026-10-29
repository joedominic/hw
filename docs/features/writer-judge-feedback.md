# Resume Writer Judge Feedback & Revision Architecture

## Overview

This document details the architectural approach and prompt engineering enhancements that enable the Resume Writer agent to effectively ingest, prioritize, and execute feedback produced by the ATS and Recruiter judges during resume tailoring and revision passes.

---

## 1. Problem Diagnosis

Prior to this implementation, the Writer agent did not effectively incorporate judge feedback during revision cycles due to four core architectural issues:
1. **Unformatted Run-On Text Blob:** Judge diagnostics from `feedback_lines()` were joined with simple commas (`", ".join(...)`), causing multi-sentence recommendations, bullet quotes with internal commas, and keyword lists to bleed together into an ambiguous wall of text.
2. **Missing System Persona Directives:** `DEFAULT_WRITER_SYSTEM` lacked any guidelines on how to interpret or execute judge feedback. The model had 5 detailed rules on length and non-deletion, but zero instructions on revision.
3. **The Anti-Hallucination Tension:** The prompt strictly instructed the Writer never to invent information beyond the factual anchor. When judges requested missing keywords or quantification metrics, the model experienced prompt conflict and systematically chose to ignore the feedback to avoid violating the strict anti-hallucination guardrail.
4. **Targeted Bullet Critiques Unresolved:** The prompt never instructed the model to search for specific text snippets identified by the Recruiter judge and reframe them using the suggested active phrasing.

---

## 2. Final Implementation Approach

The solution implements a 3-part coordination pattern across the agent runtime and prompt architecture:

### A. Structured Diagnostic Synthesis (`agents.py`)
In [`django_project/resume_app/agents.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/agents.py), the `format_judge_feedback_for_writer(state)` function extracts structured data from `state["last_resume_judge_json"]` (or fallback `feedback` lists) into a clean, markdown-formatted directive block:

1. **Target Revisions & Candidate Gaps (Numbered):**
   - Labeled as `#### 1. TARGET REVISIONS & CANDIDATE GAPS (APPLY ONLY WHERE GROUNDED IN CANDIDATE FACTS):` rather than "MANDATORY" commands. This resolves attention dissonance with Rule 1 (Zero Hallucination), directing the model to validate target gaps against candidate source facts before applying.
2. **ATS Searchability & Keyword Directives:**
   - Missing Core Keywords to integrate.
   - Explicit Grounding Rule: *"Weave these keywords into existing accomplishments or technical skills summary where related work was performed. Do not fabricate false employers or degrees."*
   - Placement strategy and formatting/parseability directives (e.g. date format standardization).
3. **Recruiter Phrasing & Quantification Directives:**
   - Targeted Bullet Phrasing Critiques: specifies the `[Role Anchor]`, the exact `Original text snippet` to locate in the draft, and the `Rewrite directive` with suggested strong action verbs.
   - Metric & Quantification Gaps: cites the role anchor and specific metric deficiency to sharpen.
   - Executive Narrative & Seniority guidance.
4. **Informative Initial Pass Placeholder:**
   - When no feedback exists (Pass 1), it outputs: `(none — initial draft generation pass. Focus on tailoring to the job description while preserving all original facts, roles, and scope.)`.

### B. Explicit Revision Execution Guardrails (`DEFAULT_WRITER_SYSTEM`)
In [`django_project/resume_app/prompts.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py), a dedicated `Revision & Judge Feedback Execution` section was added to the system prompt:
- **Rule 1:** Execute Priority Revision Commands as a checklist.
- **Rule 2:** Infuse missing ATS keywords naturally into matching existing experience bullets or skills summary without inventing false jobs or credentials.
- **Rule 3:** Locate specific bullet snippets from Recruiter critiques and rewrite them with the recommended active phrasing.
- **Rule 4:** Close metric and scale indicators for flagged quantification gaps.
- **Rule 5:** Enforce ATS date and formatting directives.

### C. Executive Style, Active Voice & Strategic Architectural Bridging (`DEFAULT_WRITER_SYSTEM`)
To eliminate the passive voice regressions caused by negative sentence-opener quotas and close the ATS ceiling on proprietary vendor platforms:
1. **Absolute Prohibition on First Person:** Banned `"I"`, `"me"`, `"my"`, `"we"`, `"our"` across the entire document (including summaries) in favor of implied third-person executive phrasing.
2. **Mandatory Active Voice & Direct Ownership:** Strictly forbidden passive auxiliary constructions (`was architected`, `were consolidated`, `was delivered`). All bullets must convey decisive leadership and technical ownership (`Architected and delivered...`, `Consolidated 5 disparate systems...`).
3. **Natural Context Flow Over Quotas:** Replaced artificial negative percentage quotas (e.g. "33% non-verb opener" rules) with a structured narrative flow: Bullet 1 establishes context/mandate, while Bullets 2+ lead with high-impact action verbs.
4. **Strategic Architectural Bridging:** For target roles demanding proprietary ecosystems absent from candidate history (e.g. Salesforce Agentforce, Snowflake), the Writer agent is instructed never to hallucinate the missing tool name, but instead foreground verified architectural equivalents (e.g., framing LangGraph/Devin as *"Autonomous Multi-Agent Orchestration & Enterprise AI Workflows"*; framing CRM unification as *"Enterprise Salesforce Ecosystem Architecture & Schema Harmonization"*).

### D. Epistemic XML Input Architecture & Recency Bias Mitigation (`DEFAULT_WRITER_USER`)
In [`django_project/resume_app/prompts.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py), inputs in `DEFAULT_WRITER_USER` are encapsulated in 10 semantic XML tags categorized into four explicit epistemic classes declared in `DEFAULT_WRITER_SYSTEM`:

1. **Candidate Ground Truth (Immutable Facts):**
   - `<candidate_source_facts>`: Uploaded resume history.
   - `<candidate_supplemental_records>`: Verified supplementary accomplishments.
   - `<retrieved_resume_bullets>`: RAG-retrieved semantic accomplishments from candidate history.
2. **Target Benchmarks (Job Desires — NOT Candidate Facts):**
   - `<target_job_description>`: Role summary and primary requirements.
   - `<target_job_description_full>`: Complete unprocessed job description.
   - `<pipeline_skills_benchmark>`: Extracted ATS skills list (directional gaps only).
3. **Current Document State:**
   - `<working_draft>`: The draft currently undergoing revision.
4. **Directives & Constraints (Priming Output Generation):**
   - `<user_guidance_notes>`: Tactical user guidance.
   - `<judge_feedback_and_revisions>`: Automated Recruiter and ATS Judge feedback.
   - `<length_and_scope_directive>`: Dynamically calculated word and bullet budgets.

**Recency Bias Prevention:** Immutable ground truth and benchmark context are anchored early (cache-friendly prefix), while active revision directives and length guardrails sit directly above the `Optimized Resume:` trigger to prime the next-token sequence.

---

## 3. Verification & Test Suite

Unit tests in [`django_project/resume_app/tests.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/tests.py) under `WriterNodePromptTestCase` verify:
1. `test_writer_node_formats_structured_judge_insights`: Confirms that `last_resume_judge_json` formats priority commands, missing keywords, bullet snippets, and rewrite directives into the LLM prompt.
2. `test_writer_node_formats_empty_feedback_informative_placeholder`: Confirms that first-pass executions without feedback display the clear initial generation note instead of blank lines.
3. `PromptStoreResolveTestCase`: Confirms prompt healing and backward compatibility for default prompt profiles.
