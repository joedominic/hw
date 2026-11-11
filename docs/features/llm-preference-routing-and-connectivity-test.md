# LLM Preference Routing & Connectivity Test

## Overview

This document specifies the unified LLM provider resolution architecture, subscription plan BYOK enforcement, OpenRouter text generation model filtering, and the in-app LLM Connectivity Test in HireEdge.

---

## 1. Problem & Context

Prior to this implementation:
1. **Redundant "Active LLM Provider" Configuration:**  
   Users were required to configure both a single "Active LLM Provider" and an ordered "Provider/Model Preference List".
2. **Optimizer Resolution Blind Spot:**  
   The Resume Optimizer preflight check ([`resolve_optimizer_provider`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/llm/session.py)) strictly inspected the single active provider. If that active provider was a local SLM (e.g. `Ollama Local`), the Optimizer rejected it and fell back directly to platform environment keys—ignoring valid cloud providers (such as Groq or OpenAI) configured in the user's preference list.
3. **Plan Fallback Bypass:**  
   Users subscribed to the `byok` (BYOK Rails) plan are barred from platform-funded keys, but preflight failed to inspect plan entitlements upfront, causing misleading UI indicators and runtime budget exceptions upon execution.
4. **OpenRouter Truncation and Modality Pollution:**  
   When fetching OpenRouter models, the provider list was cut off at 50 models alphabetically (ending at letter 'b'), causing all models starting with 'c' through 'z' (Claude, GPT-4o, Gemini, Llama, Mistral, Qwen, etc.) to be dropped. Furthermore, OpenRouter returned image generation, audio/music, and video models that could not be used for text-based resume optimization.

---

## 2. Architecture & Solution

### A. Preference-Driven Optimizer Resolution

[`resolve_optimizer_provider`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/llm/session.py) resolves execution providers using an ordered, plan-aware policy:

1. **Active Provider Check:**  
   If the user has an active provider that is cloud-grade (`not provider_is_local(byok)`), it is used.
2. **Preference List Fallback:**  
   If the active provider is local or unset, the resolver scans [`_preference_candidates(user)`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/llm/gateway.py) for the highest-priority non-local (cloud) model configured with valid user credentials.
3. **Plan-Aware Guardrail:**  
   If no user cloud model is available:
   - **BYOK Plan Users (`slug == "byok"` or `byok_required_for_completion`):** Platform-managed keys in `.env` are strictly excluded. The resolver returns `ready: False` and `blocked_by_byok_plan: True` with an explicit plan explanation:
     > *"The BYOK Rails plan requires configuring your own cloud API key in Settings -> AI / LLM. Platform-managed API keys are not included on this plan."*
   - **Platform-Funded Plans (Free / Pro / Power):** The resolver falls back to platform default cloud candidates ([`_system_default_candidates`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/llm/gateway.py)) subject to the user's daily token budget.

### B. Gateway System Default Hardening

In [`_system_default_candidates`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/llm/gateway.py), users on BYOK plans are filtered out from all non-local platform candidates upfront. This guarantees that background tasks (Huey, Vetting, Job Search) never draw against platform keys on BYOK accounts.

### C. Settings LLM Connectivity Test

The legacy "Active LLM Provider" card in Settings (`/settings/?tab=llm`) has been transformed into the **LLM Connectivity Test**:
- **Scoped Targets:** The dropdown is populated exclusively with `(Provider, Model)` entries from the user's saved **Provider/Model Preference List** (`resume_app_llmproviderpreference`).
- **Live In-App Testing:** A **Test connection** button invokes a live test prompt (`"Hello! Please reply with 'OK' if you can read this message."`) using the tenant's decrypted stored key.
- **AJAX Feedback:** Returns latency in milliseconds, status badge, and response snippet inline without page reloads.
- **Local-Only Notice:** Displays an informational banner if the user's preference list only contains local models, notifying them that cloud features (like the Optimizer) require a cloud provider.

### D. Scoped LLM Testing Console (`/resume/llm-test/`)

[`llm_test_view`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/views.py) and [`llm_test.html`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/templates/resume_app/llm_test.html) restrict provider selection to only the providers present in the user's preference list or connected with saved credentials, rather than listing unconnected global providers.

### E. OpenRouter Model Discovery & Text Generation Filtering

[`list_models_openrouter`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/llm/services.py) was enhanced to resolve OpenRouter model listing:
1. **Uncapped Listing:** Calls `_list_models_generic` with `limit=None`, eliminating the legacy 50-model alphabetical truncation and exposing the complete catalogue of 450+ models.
2. **Text-Only Modality Filtering:** Inspects `architecture.output_modalities` and `architecture.modality`:
   - Dedicated non-text models generating `image`, `audio`, or `video` (e.g. `google/gemini-3.1-flash-image`, `openai/gpt-audio`, `google/lyria-3-pro-preview`) are strictly filtered out.
   - Models generating text (`text` in `output_modalities` or modality ending with `->text`), including OpenRouter's smart routers (`openrouter/auto`), are retained.
3. **Uncapped Prompt Routing:** In [`django_project/resume_app/settings_views.py`](file:///D:/Workshop/JobApp-Main/django_project/resume_app/settings_views.py), the `[:20]` slice on cached provider models was removed, allowing tenants to map any fetched model to specific workflow prompts (Resume Writer, Ats Judge, etc.).

---

## 3. API & Contract Specifications

### Settings Test Endpoint

- **Endpoint:** `POST /settings/?tab=llm`
- **Headers:** `X-Requested-With: XMLHttpRequest`, `Accept: application/json`
- **Payload:**
  ```json
  {
    "action": "test_llm_connectivity",
    "test_pref_id": 22
  }
  ```
- **Success Response (HTTP 200):**
  ```json
  {
    "ok": true,
    "provider": "Groq",
    "model": "qwen/qwen3.8-27b",
    "latency_ms": 154,
    "response": "OK",
    "message": "Connected to Groq (qwen/qwen3.8-27b) in 154ms: OK"
  }
  ```
- **Failure Response (HTTP 400):**
  ```json
  {
    "ok": false,
    "provider": "Groq",
    "model": "qwen/qwen3.8-27b",
    "error": "Connection failed to Groq: Error code: 401 - Invalid API Key"
  }
  ```

### OpenRouter Model Listing API

- **Endpoint:** `GET /api/llm/models?provider=OpenRouter`
- **Headers:** `Cookie: sessionid=<user-session-id>`
- **Success Response (HTTP 200):**
  ```json
  {
    "models": [
      "aion-labs/aion-2.0",
      "anthropic/claude-sonnet-4.5",
      "deepseek/deepseek-chat",
      "meta-llama/llama-3.3-70b-instruct",
      "openai/gpt-4o",
      "openrouter/auto"
    ],
    "default_model": "openai/gpt-4o-mini"
  }
  ```
