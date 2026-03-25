"""One-off template styling for jobs_search.html — run from repo root or django_project."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "resume_app" / "templates" / "resume_app" / "jobs_search.html"
t = p.read_text(encoding="utf-8")

def must_sub(old: str, new: str, s: str) -> str:
    if old not in s:
        raise SystemExit(f"MISSING block:\n{old[:120]}...")
    return s.replace(old, new, 1)

t = must_sub(
    """{% block content %}
<div class="row mb-3">
    <div class="col-12">
        <h1 class="mb-3">Job search</h1>
        <p class="text-muted">
            Search for jobs, save favourites, run fit checks, and track matches.
        </p>
    </div>
</div>

{% if messages %}
    <div class="row mb-3">
        <div class="col-12">
            {% for message in messages %}
                <div class="alert alert-{{ message.tags|default:'info' }} mb-2" role="alert">
                    {{ message }}
                </div>
            {% endfor %}
        </div>
    </div>
{% endif %}""",
    """{% block content %}
<div class="mx-auto max-w-7xl">
<div class="mb-8">
        <h1 class="text-2xl font-bold tracking-tight text-concierge-slate">Job search</h1>
        <p class="mt-1 text-sm text-slate-600">
            Search for jobs, save favourites, run fit checks, and track matches.
        </p>
    </div>

{% include "resume_app/_concierge_messages.html" %}""",
    t,
)

t = must_sub(
    """{% if disqualifier_prompt %}
<div class="row mb-3">
    <div class="col-12">
        <div class="card">
            <div class="card-header">
                Add disqualifier from "{{ disqualifier_prompt.title }}" @ {{ disqualifier_prompt.company_name }}
            </div>
            <div class="card-body">
                <p class="text-muted small mb-2">Add a word or phrase from the description below to your disqualifiers, or use the section above anytime.</p>
                <details>
                    <summary class="small text-muted">Show job description</summary>
                    <pre class="small bg-light p-2 mt-1 mb-0" style="max-height:200px; overflow:auto;">{{ disqualifier_prompt.description }}</pre>
                </details>
            </div>
        </div>
    </div>
</div>
{% endif %}""",
    """{% if disqualifier_prompt %}
<div class="mb-6 rounded-concierge border border-amber-200 bg-amber-50/30 p-4 shadow-sm">
            <h2 class="text-sm font-semibold text-concierge-slate">Add disqualifier from "{{ disqualifier_prompt.title }}" @ {{ disqualifier_prompt.company_name }}</h2>
                <p class="mt-2 text-xs text-slate-600">Add a phrase from the description, or use the section below anytime.</p>
                <details class="mt-2">
                    <summary class="cursor-pointer text-xs font-medium text-slate-500">Show job description</summary>
                    <pre class="mt-2 max-h-48 overflow-auto rounded-concierge border border-slate-200 bg-slate-50 p-2 text-xs">{{ disqualifier_prompt.description }}</pre>
                </details>
</div>
{% endif %}""",
    t,
)

t = must_sub(
    """<div class="row mb-3" id="disqualifiers-section">
    <div class="col-12">
        <div class="card">
            <div class="card-header py-2">
                <span class="small fw-bold">Your disqualifiers</span>
                <span class="small text-muted">(jobs containing any of these are hidden — add or remove anytime)</span>
            </div>
            <div class="card-body py-2">
                <form id="disqualifier-add-form" class="d-flex flex-wrap align-items-end gap-2 mb-2">
                    <div class="flex-grow-1" style="min-width:200px;">
                        <label class="form-label small visually-hidden" for="disqualifier-phrase-input">Phrase to avoid</label>
                        <input type="text" class="form-control form-control-sm" name="phrase" id="disqualifier-phrase-input" placeholder="e.g. no visa sponsorship" maxlength="500" autocomplete="off">
                    </div>
                    <button type="submit" class="btn btn-sm btn-primary">Add</button>
                </form>
                <div id="disqualifiers-list" class="d-flex flex-wrap gap-1">
                    {% for d in current_disqualifiers %}
                    <span class="badge bg-secondary d-inline-flex align-items-center gap-1 disqualifier-badge" data-id="{{ d.id }}">
                        <span class="disqualifier-phrase">{{ d.phrase|truncatechars:50 }}</span>
                        <button type="button" class="btn btn-link btn-sm p-0 text-white disqualifier-remove" style="font-size:0.85em;" title="Remove" aria-label="Remove">&times;</button>
                    </span>
                    {% empty %}
                    <span class="text-muted small" id="disqualifiers-empty">No disqualifiers yet.</span>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>
</div>""",
    """<div class="mb-6 rounded-concierge border border-slate-200 bg-white p-4 shadow-sm" id="disqualifiers-section">
            <div class="mb-1 text-sm font-semibold text-concierge-slate">Your disqualifiers</div>
            <p class="mb-3 text-xs text-slate-500">Jobs containing any phrase below are hidden.</p>
                <form id="disqualifier-add-form" class="mb-3 flex flex-wrap items-end gap-2">
                    <div class="min-w-[200px] flex-1">
                        <label class="sr-only" for="disqualifier-phrase-input">Phrase to avoid</label>
                        <input type="text" class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm" name="phrase" id="disqualifier-phrase-input" placeholder="e.g. no visa sponsorship" maxlength="500" autocomplete="off">
                    </div>
                    <button type="submit" class="rounded-concierge bg-concierge-slate px-3 py-2 text-sm font-semibold text-white hover:bg-slate-800">Add</button>
                </form>
                <div id="disqualifiers-list" class="flex flex-wrap gap-2">
                    {% for d in current_disqualifiers %}
                    <span class="disqualifier-badge inline-flex items-center gap-1 rounded-full bg-slate-700 px-2 py-1 text-xs font-medium text-white" data-id="{{ d.id }}">
                        <span class="disqualifier-phrase">{{ d.phrase|truncatechars:50 }}</span>
                        <button type="button" class="disqualifier-remove rounded px-1 text-white/90 hover:bg-white/20 hover:text-white" title="Remove" aria-label="Remove">&times;</button>
                    </span>
                    {% empty %}
                    <span class="text-sm text-slate-500" id="disqualifiers-empty">No disqualifiers yet.</span>
                    {% endfor %}
                </div>
</div>""",
    t,
)

t = t.replace(
    "badge.className = 'badge bg-secondary d-inline-flex align-items-center gap-1 disqualifier-badge';",
    "badge.className = 'disqualifier-badge inline-flex items-center gap-1 rounded-full bg-slate-700 px-2 py-1 text-xs font-medium text-white';",
    1,
)
old_inner = (
    "badge.innerHTML = '<span class=\"disqualifier-phrase\">' + phraseText.replace(/</g, '&lt;').replace(/>/g, '&gt;') + "
    "'</span> <button type=\"button\" class=\"btn btn-link btn-sm p-0 text-white disqualifier-remove\" "
    "style=\"font-size:0.85em;\" title=\"Remove\" aria-label=\"Remove\">&times;</button>';"
)
new_inner = (
    "badge.innerHTML = '<span class=\"disqualifier-phrase\">' + phraseText.replace(/</g, '&lt;').replace(/>/g, '&gt;') + "
    "'</span> <button type=\"button\" class=\"disqualifier-remove rounded px-1 text-white/90 hover:bg-white/20\" "
    "title=\"Remove\" aria-label=\"Remove\">&times;</button>';"
)
if old_inner not in t:
    raise SystemExit("badge innerHTML pattern missing")
t = t.replace(old_inner, new_inner, 1)

t = t.replace("span.className = 'text-muted small';", "span.className = 'text-sm text-slate-500';", 1)

t = t.replace(
    "likeBtn.classList.remove('btn-outline-primary');\n"
    "                            likeBtn.classList.add('btn-primary');",
    "likeBtn.className = 'rounded-concierge bg-concierge-slate px-2 py-1 text-xs font-medium text-white job-like-btn';",
    1,
)

t = t.replace(
    "el.className = 'ms-2 ' + (isError ? 'text-danger fw-bold' : 'text-muted');",
    "el.className = 'ms-2 text-sm ' + (isError ? 'font-semibold text-red-600' : 'text-slate-500');",
    1,
)

t = t.replace(
    "if (spinner) spinner.classList.toggle('visually-hidden', !loading);",
    "if (spinner) spinner.classList.toggle('hidden', !loading);",
    1,
)

old_insights = (
    "assessBody.innerHTML = '<p class=\"text-muted mb-0\">"
    "<span class=\"spinner-border spinner-border-sm me-2\" role=\"status\" aria-hidden=\"true\"></span>"
    "Running Insights…</p>';"
)
new_insights = (
    "assessBody.innerHTML = '<p class=\"mb-0 flex items-center gap-2 text-sm text-slate-500\">"
    "<span class=\"inline-block h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-concierge-slate\"></span>"
    "Running Insights…</p>';"
)
t = t.replace(old_insights, new_insights, 1)

t = t.replace('<div class="modal-content">', '<div class="modal-content rounded-concierge border border-slate-200 shadow-xl">', 1)

# Search card + two-column layout
t = must_sub(
    """<!-- Primary search + filters -->
<div class="row mb-3">
    <div class="col-12">
        <div class="card">
            <div class="card-body">
                <form method="get" class="row g-2 align-items-end">""",
    """<!-- Primary search + filters -->
<div class="mb-6 rounded-concierge border border-slate-200 bg-white p-4 shadow-sm sm:p-6">
                <form method="get" class="grid gap-3 sm:grid-cols-2 lg:grid-cols-12 lg:items-end">""",
    t,
)

# Labels + inputs in search form — replace form-control groups
replacements = [
    (
        """                    <div class="col-md-6 col-lg-3">
                        <label class="form-label" for="search-term-input">Search term</label>
                        <input type="text" class="form-control" id="search-term-input" name="q"
                               value="{{ query }}" placeholder="e.g. Python developer">
                    </div>""",
        """                    <div class="sm:col-span-1 lg:col-span-3">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="search-term-input">Search term</label>
                        <input type="text" class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm" id="search-term-input" name="q"
                               value="{{ query }}" placeholder="e.g. Python developer">
                    </div>""",
    ),
    (
        """                    <div class="col-md-6 col-lg-3">
                        <label class="form-label" for="location-input">Location (optional)</label>
                        <input type="text" class="form-control" id="location-input" name="location"
                               value="{{ location }}" placeholder="e.g. Dallas, TX">
                    </div>""",
        """                    <div class="sm:col-span-1 lg:col-span-3">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="location-input">Location (optional)</label>
                        <input type="text" class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm" id="location-input" name="location"
                               value="{{ location }}" placeholder="e.g. Dallas, TX">
                    </div>""",
    ),
    (
        """                    <div class="col-md-6 col-lg-2">
                        <label class="form-label" for="track-select">Target track</label>
                        <select id="track-select" name="track" class="form-select">""",
        """                    <div class="sm:col-span-1 lg:col-span-2">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="track-select">Target track</label>
                        <select id="track-select" name="track" class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm">""",
    ),
    (
        """                    <div class="col-md-6 col-lg-3">
                        <label class="form-label" for="resume-select">Resume for match</label>
                        <select id="resume-select" name="resume_id" class="form-select">""",
        """                    <div class="sm:col-span-1 lg:col-span-3">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="resume-select">Resume for match</label>
                        <select id="resume-select" name="resume_id" class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm">""",
    ),
    (
        """                    <div class="col-md-6 col-lg-2">
                        <label class="form-label" for="min-score-input">Min score</label>
                        <input type="number" min="0" max="100" id="min-score-input" name="min_score"
                               class="form-control" value="{{ min_score }}">
                    </div>""",
        """                    <div class="sm:col-span-1 lg:col-span-2">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="min-score-input">Min score</label>
                        <input type="number" min="0" max="100" id="min-score-input" name="min_score"
                               class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm" value="{{ min_score }}">
                    </div>""",
    ),
    (
        """                    <div class="col-md-6 col-lg-2">
                        <label class="form-label" for="results-wanted-input">Jobs to fetch</label>
                        <input type="number" min="10" max="200" id="results-wanted-input" name="results_wanted"
                               class="form-control" value="{{ results_wanted }}" title="Number of jobs to fetch from job search (10–200)">
                    </div>""",
        """                    <div class="sm:col-span-1 lg:col-span-2">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="results-wanted-input">Jobs to fetch</label>
                        <input type="number" min="10" max="200" id="results-wanted-input" name="results_wanted"
                               class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm" value="{{ results_wanted }}" title="Number of jobs to fetch from job search (10–200)">
                    </div>""",
    ),
    (
        """                    <div class="col-md-6 col-lg-2">
                        <label class="form-label" for="llm-model-select">Model ({{ job_search_llm_provider }})</label>
                        <select id="llm-model-select" name="llm_model" class="form-select" title="Used for LLM match score (Matching prompt)">""",
        """                    <div class="sm:col-span-1 lg:col-span-2">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500" for="llm-model-select">Model ({{ job_search_llm_provider }})</label>
                        <select id="llm-model-select" name="llm_model" class="w-full rounded-concierge border border-slate-200 px-3 py-2 text-sm" title="Used for LLM match score (Matching prompt)">""",
    ),
    (
        """                    <div class="col-md-6 col-lg-2">
                        <label class="form-label">Model (Match)</label>
                        <div class="form-control text-muted">Configure provider in Settings</div>
                    </div>""",
        """                    <div class="sm:col-span-1 lg:col-span-2">
                        <label class="mb-1 block text-xs font-semibold uppercase text-slate-500">Model (Match)</label>
                        <div class="rounded-concierge border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500">Configure provider in Settings</div>
                    </div>""",
    ),
    (
        """                    <div class="col-md-4 col-lg-1 text-md-end">
                        <label class="form-label d-none d-md-block">&nbsp;</label>
                        <button type="submit" class="btn btn-primary w-100">Search</button>
                    </div>
                </form>
            </div>
        </div>
    </div>
</div>""",
        """                    <div class="flex items-end sm:col-span-2 lg:col-span-1">
                        <button type="submit" class="w-full rounded-concierge bg-concierge-slate px-4 py-2 text-sm font-semibold text-white shadow-sm hover:bg-slate-800 lg:w-auto">Search</button>
                    </div>
                </form>
</div>""",
    ),
]

for old, new in replacements:
    t = must_sub(old, new, t)

t = must_sub(
    """<div class="row">
    <!-- Left: results / favourites / excluded -->
    <div class="col-lg-8 mb-4">
        <div class="card">
            <div class="card-header border-bottom-0 pb-0">
                <ul class="nav nav-tabs card-header-tabs">""",
    """<div class="grid gap-6 lg:grid-cols-12">
    <div class="lg:col-span-8">
        <div class="rounded-concierge border border-slate-200 bg-white shadow-sm">
            <div class="border-b border-slate-100 px-2 pt-2">
                <ul class="nav nav-tabs flex flex-wrap gap-1 border-0">""",
    t,
)

t = must_sub(
    """                {% if not show_favourites and not show_excluded and query and search_results %}
                <div class="card-body pt-0 pb-1">
                    <div class="d-flex flex-wrap align-items-center gap-3 mb-1">
                        <span class="small text-muted">Sort by:</span>""",
    """                {% if not show_favourites and not show_excluded and query and search_results %}
                <div class="border-b border-slate-100 px-4 py-2">
                    <div class="flex flex-wrap items-center gap-3 text-xs">
                        <span class="text-slate-500">Sort by:</span>""",
    t,
)

t = t.replace(
    """                        <a href="{% url 'jobs_search' %}?view=results&q={{ query|urlencode }}&location={{ location|urlencode }}&track={{ job_search_track }}&resume_id={{ selected_resume_id }}&min_score={{ min_score }}&results_wanted={{ results_wanted }}&llm_model={{ job_search_llm_model|default:''|urlencode }}&sort=focus"
                           class="small {% if sort_param == 'focus' %}fw-bold{% endif %}">Focus</a>
                        <span class="small text-muted"> · </span>
                        <a href="{% url 'jobs_search' %}?view=results&q={{ query|urlencode }}&location={{ location|urlencode }}&track={{ job_search_track }}&resume_id={{ selected_resume_id }}&min_score={{ min_score }}&results_wanted={{ results_wanted }}&llm_model={{ job_search_llm_model|default:''|urlencode }}&refresh=1"
                           class="small">Refresh results</a>
                        <span class="small text-muted ms-2">|</span>
                        <span class="small text-muted">Insights:</span>
                        <a href="#" id="insights-select-all" class="small" title="Check all job rows">Select all</a>
                        <a href="#" id="insights-deselect-all" class="small" title="Uncheck all">Deselect all</a>
                        <button type="button" class="btn btn-sm btn-success" id="insights-btn" title="Get LLM insights across selected job descriptions">
                            <span id="insights-btn-text">Insights</span>
                            <span id="insights-spinner" class="spinner-border spinner-border-sm ms-1 visually-hidden" role="status" aria-hidden="true"></span>
                        </button>""",
    """                        <a href="{% url 'jobs_search' %}?view=results&q={{ query|urlencode }}&location={{ location|urlencode }}&track={{ job_search_track }}&resume_id={{ selected_resume_id }}&min_score={{ min_score }}&results_wanted={{ results_wanted }}&llm_model={{ job_search_llm_model|default:''|urlencode }}&sort=focus"
                           class="font-semibold text-concierge-slate {% if sort_param != 'focus' %}font-normal text-slate-600{% endif %}">Focus</a>
                        <span class="text-slate-300"> · </span>
                        <a href="{% url 'jobs_search' %}?view=results&q={{ query|urlencode }}&location={{ location|urlencode }}&track={{ job_search_track }}&resume_id={{ selected_resume_id }}&min_score={{ min_score }}&results_wanted={{ results_wanted }}&llm_model={{ job_search_llm_model|default:''|urlencode }}&refresh=1"
                           class="text-slate-600 hover:text-concierge-slate">Refresh</a>
                        <span class="text-slate-300">|</span>
                        <span class="text-slate-500">Insights:</span>
                        <a href="#" id="insights-select-all" class="text-concierge-slate hover:underline" title="Check all job rows">Select all</a>
                        <a href="#" id="insights-deselect-all" class="text-concierge-slate hover:underline" title="Uncheck all">Deselect all</a>
                        <button type="button" class="rounded-concierge bg-emerald-600 px-2 py-1 text-xs font-semibold text-white hover:bg-emerald-700" id="insights-btn" title="Get LLM insights across selected job descriptions">
                            <span id="insights-btn-text">Insights</span>
                            <span id="insights-spinner" class="ml-1 hidden inline-block h-3 w-3 animate-spin rounded-full border-2 border-white/40 border-t-white" role="status" aria-hidden="true"></span>
                        </button>""",
    1,
)

t = must_sub(
    """            <div class="card-body">""",
    """            <div class="p-4 sm:p-5">""",
    t,
    # first occurrence only for results card - might match multiple times
)

# Fix: must_sub first card-body - there could be several. Use more context
t = t.replace(
    """                </ul>
                {% if not show_favourites and not show_excluded and query and search_results %}""",
    """                </ul>
                {% if not show_favourites and not show_excluded and query and search_results %}""",
    1,
)

# Replace first "            <div class=\"card-body\">" after nav-tabs closing - fragile
marker = """                {% endif %}
            </div>
            <div class="card-body">"""
if marker in t:
    t = t.replace(marker, """                {% endif %}
            </div>
            <div class="p-4 sm:p-5">""", 1)
else:
    # try alternate without endif
    t = t.replace(
        """            </div>
            <div class="card-body">
                {% if show_favourites %}""",
        """            </div>
            <div class="p-4 sm:p-5">
                {% if show_favourites %}""",
        1,
    )

# Right column + closings
t = must_sub(
    """    <!-- Right: matches -->
    <div class="col-lg-4 mb-4">
        <div class="card mb-3">
            <div class="card-header">
                My matches
            </div>
            <div class="card-body">""",
    """    <div class="lg:col-span-4">
        <div class="rounded-concierge border border-slate-200 bg-white shadow-sm">
            <div class="border-b border-slate-100 px-4 py-3 text-sm font-semibold text-concierge-slate">
                My matches
            </div>
            <div class="p-4">""",
    t,
)

t = must_sub(
    """            </div>
        </div>
    </div>
</div>

<!-- LLM Assessment modal""",
    """            </div>
        </div>
    </div>
</div>

<!-- LLM Assessment modal""",
    t,
)

# Add closing wrapper div for mx-auto before modal
t = t.replace(
    """</div>

<!-- LLM Assessment modal (per-job "Assess" button) -->""",
    """</div>
</div>

<!-- LLM Assessment modal (per-job "Assess" button) -->""",
    1,
)

# Bulk button / card / badge shortcuts for remaining bootstrap in job rows
simple = [
    ('class="border rounded p-2 mb-2"', 'class="mb-3 rounded-concierge border border-slate-200 bg-white p-3 shadow-sm"'),
    ('class="border rounded p-2 mb-2 job-result-row"', 'class="job-result-row mb-3 rounded-concierge border border-slate-200 bg-white p-3 shadow-sm"'),
    ('class="btn btn-sm btn-primary"', 'class="rounded-concierge bg-concierge-slate px-2 py-1 text-xs font-semibold text-white hover:bg-slate-800"'),
    ('class="btn btn-sm btn-outline-secondary job-assess-btn"', 'class="job-assess-btn rounded-concierge border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-concierge-slate hover:bg-slate-50"'),
    ('class="btn btn-sm btn-outline-success"', 'class="rounded-concierge border border-emerald-200 bg-white px-2 py-1 text-xs font-medium text-emerald-800 hover:bg-emerald-50"'),
    ('class="btn btn-sm btn-outline-secondary mb-1"', 'class="mb-1 rounded-concierge border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-concierge-slate hover:bg-slate-50"'),
    ('class="btn btn-sm btn-outline-danger mb-1"', 'class="mb-1 rounded-concierge border border-red-200 bg-white px-2 py-1 text-xs font-medium text-red-800 hover:bg-red-50"'),
    ('class="btn btn-sm btn-outline-primary mb-1"', 'class="mb-1 rounded-concierge border border-sky-200 bg-white px-2 py-1 text-xs font-medium text-sky-900 hover:bg-sky-50"'),
    ('class="btn btn-sm btn-outline-primary job-like-btn"', 'class="job-like-btn rounded-concierge border border-sky-200 bg-white px-2 py-1 text-xs font-medium text-sky-900 hover:bg-sky-50"'),
    ('class="btn btn-sm btn-outline-danger job-dislike-btn"', 'class="job-dislike-btn rounded-concierge border border-red-200 bg-white px-2 py-1 text-xs font-medium text-red-800 hover:bg-red-50"'),
    ('class="btn btn-sm btn-outline-info py-0"', 'class="rounded-concierge border border-sky-200 px-2 py-0.5 text-xs font-medium text-sky-900 hover:bg-sky-50"'),
    ('class="badge bg-info text-dark"', 'class="rounded-full bg-sky-100 px-2 py-0.5 text-xs font-medium text-sky-900"'),
    ('class="badge bg-info text-dark ms-1"', 'class="ml-1 rounded-full bg-sky-100 px-2 py-0.5 text-xs font-medium text-sky-900"'),
    ('class="badge bg-success ms-1"', 'class="ml-1 rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-900"'),
    ('class="badge bg-danger text-light"', 'class="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-900"'),
    ('class="badge bg-success text-light"', 'class="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-900"'),
    ('class="badge bg-secondary text-light"', 'class="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700"'),
    ('class="badge bg-primary"', 'class="rounded-full bg-concierge-slate px-2 py-0.5 text-xs font-medium text-white"'),
    ('class="badge bg-secondary"', 'class="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700"'),
    ('class="badge bg-success"', 'class="rounded-full bg-emerald-100 px-2 py-0.5 text-xs font-medium text-emerald-900"'),
    ('class="form-check-input insights-job-cb"', 'class="insights-job-cb mt-1 h-4 w-4 rounded border-slate-300 text-concierge-slate focus:ring-concierge-slate"'),
    ('class="btn btn-secondary"', 'class="rounded-concierge border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"'),
]
for a, b in simple:
    t = t.replace(a, b)

# Nav tab badges in results header
t = t.replace('class="badge rounded-pill bg-secondary ms-1"', 'class="ml-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600"',)

p.write_text(t, encoding="utf-8")
print("OK:", p)
