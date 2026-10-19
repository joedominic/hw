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

1. **Priority Revision Commands (Numbered):**
   - Extracted directly from `synthesis.priority_revisions` to serve as the Writer's primary revision checklist.
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

### C. Elevated Prompt Prominence (`DEFAULT_WRITER_USER`)
In [`django_project/resume_app/prompts.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/prompts.py), `{feedback}` was moved from the bottom of the prompt to a prominent position directly after the Length & Scope Directive and immediately before the Job Description and supporting context, labeled as:
```text
Judge Feedback & Revision Directives (Apply All Actions When Present):
{feedback}
```

---

## 3. Verification & Test Suite

Unit tests in [`django_project/resume_app/tests.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/tests.py) under `WriterNodePromptTestCase` verify:
1. `test_writer_node_formats_structured_judge_insights`: Confirms that `last_resume_judge_json` formats priority commands, missing keywords, bullet snippets, and rewrite directives into the LLM prompt.
2. `test_writer_node_formats_empty_feedback_informative_placeholder`: Confirms that first-pass executions without feedback display the clear initial generation note instead of blank lines.
3. `PromptStoreResolveTestCase`: Confirms prompt healing and backward compatibility for default prompt profiles.
