# Streamlining & Optimization Proposals

This document outlines proposed improvements to the job pipeline to reduce noise, lower costs, and better leverage local AI.

## 1. Hybrid Ranking: Vector + Local LLM (Ollama)

### The Problem
SentenceTransformers are great at semantic similarity but often fail on "negatives" or seniority levels (e.g., "Senior Software Engineer" and "Junior Software Engineer" have high cosine similarity because they share most words).

### The Solution: "Ollama Guard"
Instead of replacing the vector search, we introduce a fast, local LLM check using **Nemotron 4B** as a secondary filter in the initial pipeline stage.

**Proposed Flow:**
1. **Vector Screen**: Fetch 50 jobs, rank by preference margin (as we do now).
2. **Ollama Filter (Top 20)**: Take the top 20 jobs and run a "Fast Fit Check" via local Ollama.
   - **Prompt**: "Does this Job Title and snippet imply a [Principal/Director] level role? Answer YES/NO with a 1-sentence reason."
   - **Action**: If Ollama says NO, demote the job's score or move it to a 'Low Fit' hidden tab.

### Tradeoffs
- **Latency**: Adding Ollama to the fetch process will add ~30-60 seconds to a background task run. Since these run asynchronously via Huey, this is a highly acceptable tradeoff for higher signal.
- **Accuracy**: Nemotron 4B is small but specialized enough to distinguish "Principal" from "Junior" better than a vector centroid.

---

## 2. Advanced JD Cleansing (Token Reduction)

### The Problem
Job descriptions are 50-70% boilerplate (Company mission, Benefits, EEO statements). Sending this to external LLMs (GPT-4, Claude) wastes tokens and cost.

### The Solution: `JDCleanserService`
Enhance the existing heuristics in `embeddings.py` into a robust service used by all LLM-bound tasks.

**Logic:**
1. **Header Identification**: Remove everything after headers like "Benefits", "Equal Opportunity", "Physical Requirements".
2. **Boilerplate Scrubbing**: Strip sentences containing "competitive salary", "medical, dental, vision", "world-class culture".
3. **Structured Extraction**: Prioritize "Responsibilities" and "Requirements" sections.

**Expected Benefit**: 40-60% reduction in JD token count with zero loss in "fit" signal.

---

## 3. Streamlined Pipeline Stages

### Proposed "Fast-Track" Flow
If a job scores high on both the Vector Margin (> 40) AND the Ollama Guard (YES), it should **auto-promote past Vetting directly to Applying**.

**Updated Flow:**
1. **Pipeline (Ingest)**: Vector Rank -> Ollama Guard (Top 20).
2. **Auto-Promote**: If (Margin > 40 AND Ollama=YES) -> Move to **Applying**.
3. **Vetting (Optional)**: If (Margin 10-40 OR Ollama=Uncertain) -> Move to **Vetting** for human review or heavier LLM evaluation.
4. **Discard**: If (Margin < 0 OR Ollama=NO) -> Move to **Deleted**.

### Benefit
Reduces the manual burden of clicking "Save" on jobs that are obviously good fits, while keeping the Vetting stage for "maybe" cases.
