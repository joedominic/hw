"""Shared helpers for resume_app (e.g. cron description for UI)."""


def format_job_source_label(source: str | None) -> str:
    """
    Human-readable board name for JobSpy `source` values, e.g. jobspy_indeed → Indeed.
    """
    if not source:
        return "—"
    s = str(source).strip().lower()
    if s.startswith("jobspy_"):
        s = s[7:]
    # ziprecruiter is stored as jobspy_ziprecruiter → ziprecruiter
    return s.replace("_", " ").title()


def cron_to_short_description(cron: str) -> str:
    """
    Return a short human-readable description for common cron expressions.
    Falls back to the raw expression if not recognised.
    """
    if not cron or not isinstance(cron, str):
        return cron or ""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    # Daily at specific time (e.g. 0 9 * * *)
    if dom == "*" and month == "*" and dow == "*":
        if hour.isdigit() and minute.isdigit():
            return f"Daily {int(hour):02d}:{int(minute):02d}"
        if hour == "*" and minute == "0":
            return "Every hour"
    # Every N hours
    if hour.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        try:
            n = int(hour[2:])
            if n == 1:
                return "Every hour"
            if minute == "0":
                return f"Every {n} hours"
            return f"Every {n}h at :{minute}"
        except ValueError:
            pass
    # Every N minutes (less common for job search)
    if hour == "*" and minute.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        try:
            n = int(minute[2:])
            return f"Every {n} min"
        except ValueError:
            pass
    return cron
