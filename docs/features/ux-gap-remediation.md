# UX Gap Remediation (Nov 2026)

Shipped product fixes from the Principal PM audit of HireEdge first-run and daily triage friction.

## What changed

| Gap | Fix |
| --- | --- |
| Optimizer blocked without BYOK while Getting Started said “AI ready” | Optimizer resolves platform/env keys via `resolve_optimizer_provider`; `optimize_resume` uses `resolve_provider_api_key` |
| Free daily token cap too low for one tailor run | Free `llm_tokens_per_day` raised **8,300 → 50,000** (~1.5M/mo); Run button shows preflight estimate |
| Competing Getting Started / Cockpit gate / Profiler | Post-login → **Career Profiler**; Getting Started redirects; Cockpit profile gate removed |
| Incomplete Search Profiles | Normal tenants must bind a resume (auto-binds latest library resume when omitted) |
| Save-to-Cockpit before profile | `jobs_save` returns 400 without a Search Profile; first provision migrates orphan `general` pipeline rows |
| Email verification wall | Dedicated `/accounts/verify-email/` interstitial (resend + Settings link) |
| Like / Dislike / Delete confusion | Direct 1-click **Pass (Thumbs Down)** (removes + trains AI to avoid similar roles) & **Remove (X)** (removes from pipeline only without training); rich explanatory tooltips on hover |
| Jargon | Removed Executive nav badge; Find jobs “Rank by my profile”; run modal “Skipped” |
| Retention POST vs UI | `update_pipeline_retention` requires Advanced/staff |

### Pipeline Job Card Triage Actions
To minimize clicks and eliminate ambiguity between training vs non-training removals, opportunity cards in Career Cockpit and Pipeline boards expose three dedicated 1-click buttons:
- **Like (Thumbs Up):** Improves ranking and seeds positive preference vectors for similar roles.
- **Not Interested (Thumbs Down):** Removes the job from the pipeline and trains the recommendation model to avoid similar roles in your feed.
- **Remove (X):** Removes the job from the pipeline immediately without affecting your AI recommendations or match scoring.
Both negative actions include contextual hover tooltips explaining the difference.


## Key modules

- [`resume_app/llm/session.py`](../../django_project/resume_app/llm/session.py) — `resolve_optimizer_provider`
- [`resume_app/llm/policy.py`](../../django_project/resume_app/llm/policy.py) — `optimizer_token_preflight`
- [`resume_app/experience.py`](../../django_project/resume_app/experience.py) — Profiler post-login; bound-profile onboarding
- [`resume_app/search_profile_scope.py`](../../django_project/resume_app/search_profile_scope.py) — `migrate_orphan_default_track_entries`
- Migration `0070_raise_free_plan_daily_tokens`

## Tests

```powershell
.\env\Scripts\python.exe django_project\manage.py test resume_app.test_onboarding resume_app.test_optimizer_platform_keys resume_app.test_llm_policy resume_app.test_saved_searches
```
