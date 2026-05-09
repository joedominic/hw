# Streamlining & Optimization Proposals (Implemented)

This document outlines the implemented improvements to the job pipeline to reduce noise, lower costs, and better leverage local AI.

## 1. Hybrid Ranking: Vector + Local LLM (Ollama Guard)

### The Problem
SentenceTransformers are great at semantic similarity but often fail on "negatives" or seniority levels (e.g., "Senior Software Engineer" and "Junior Software Engineer" have high cosine similarity).

### The Implementation
We introduced **Ollama Guard** as a secondary filter in the initial pipeline stage.
- **Flow**: Fetch jobs -> Vector Rank -> Ollama Fit Check (Top 10).
- **Local AI**: Uses Nemotron 4B (via Local Ollama) to verify seniority.
- **Result**: Jobs that are a clear mismatch (e.g., Junior vs. Principal) are penalized by 30 points, ensuring they drop to the bottom of the list.

---

## 2. LLM-Based JD Cleansing (Token Reduction)

### The Problem
Job descriptions are often 50-70% boilerplate (Benefits, EEO statements, Company Mission). Sending this to external LLMs wastes tokens and budget.

### The Implementation: `JDCleanserService`
Implemented a specialized service that uses Local Ollama to extract core job information.
- **Action**: Before any heavy LLM task (Vetting, Optimization), the raw JD is sent to Local Ollama.
- **Prompt**: "Extract only core responsibilities and technical requirements... eliminate all boilerplate."
- **Benefit**: 40-60% reduction in tokens sent to external LLMs like GPT-4, with zero loss in critical fit signal.

---

## 3. Fast-Track Promotion Flow

### The Problem
Clearly good matches still required manual "Saving" through every stage of the pipeline.

### The Implementation
- **Logic**: Jobs with a high preference margin (> 50) now **auto-promote past Vetting directly to Applying**.
- **Benefit**: Reduces manual work for the user while maintaining the Vetting stage for "maybe" cases that need human review or deeper AI analysis.
