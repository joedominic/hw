# Tenant Experience Simplification

**Status:** Implemented (Nov 2026) + cognitive-load pass  
**Persona:** Tenant (Normal). Staff / Power keep advanced chrome.

## Summary

Cockpit is the single operational home for tenants. Legacy stage boards, Fit Inspector, interview logging, and automation entry points fold into Cockpit (with deep-link drawers). Search Profile is the tenant-facing name; `?track=` remains accepted.

## Behavior changes

| Area | Change |
|------|--------|
| Post-login / onboarding | Incomplete tenants → `/jobs/profiler/`; complete → `/jobs/cockpit/` |
| Legacy URLs | `/jobs/pipeline\|vetting\|applying\|done/` → Cockpit `?stage=` + full query forward; `/getting-started/` → Profiler |
| Empty Cockpit | Kickoff: **Find jobs in Target & Discover** + Edit Search Profile (no profile-create gate) |
| First Search Profile | Career Profiler provision with **required** bound resume |
| Naming | **Search Profiles** everywhere; Target & Discover tab; **Find jobs** (not Search Sandbox); Tracker page title matches nav |
| Fit Inspector | Staff-only in Tools. Tenants with `job_id` land on Cockpit `drawer=inspector` (`stage=vetting` unless `stage` was already set). Without `job_id` they land on Cockpit New |
| Tracker | Momentum KPIs; Fit Audit → **Open in Cockpit**; interview CTAs deep-link Applied |
| Automation policies | Any logged-in user can open the drawer (`/jobs/automation/` or `?drawer=automation`) and POST `update_pipeline_retention` for their own account. The left-rail Pipeline Policies card stays staff and Advanced only. |
| `/jobs/tracks/` | Normal tenants redirect to Cockpit; Sandbox sidebar → **Manage in Cockpit** |
| Optimizer | Engine knobs under **Advanced Engine Settings**; Tools → Tailor deep-links Cockpit Applying; Step 2 diff workbench features a single Save Draft button next to Export PDF with asynchronous AJAX persistence (no full-page reload). |
| Plan & usage (`/billing/`) | Live plan + meters; Stripe only when configured; quota errors say Plan & usage |
| Cockpit feedback | Flash messages render; soft quota strip (≥80% / exhausted) → Plan & usage |
| Save vs Like | Primary CTA **Save to Cockpit**; Like tooltips clarify ranking-only |
| Query params | Cockpit links emit `?profile=`; backend still accepts `?track=` |

## Deep-link drawers

`/jobs/cockpit/?drawer=inspector|automation|interview[&job_id=…]`

## Canonical tenant vocabulary

- **Target & Discover** — nav tab (Profiler + Find jobs)
- **Find jobs** — search surface under that tab
- **Search Profile** — targeting unit (never “track” in tenant chrome)
- **Cockpit** — pipeline home
- **Tracker** — outcomes dashboard
- **Save to Cockpit** — put a role on the board (Review)
- **Plan & usage** — quotas / plan (Tools)

## Related specs

- [Site Functionality](../site-functionality.md)
- [Career Cockpit](../career-cockpit.md)
