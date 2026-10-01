"""Shared helpers for resume_app (e.g. cron description for UI)."""


def format_job_source_label(source: str | None) -> str:
    """
    Human-readable board name for job `source` values, e.g. jobspy_indeed → Indeed, adzuna → Adzuna.
    """
    if not source:
        return "—"
    s = str(source).strip().lower()
    if s == "adzuna":
        return "Adzuna"
    if s == "dice":
        return "Dice"
    if s == "levels":
        return "Levels.fyi"
    if s == "builtin":
        return "Built In"
    if s == "greenhouse":
        return "Greenhouse"
    if s.startswith("jobspy_"):
        s = s[7:]
    return s.replace("_", " ").title()


def format_cron_human_friendly(cron: str) -> str:
    """
    Return a polished, human-readable natural language representation of a 5-part cron expression.
    e.g. '30 12 * * *' -> 'Daily at 12:30 PM'
         '0 7 * * *'   -> 'Daily at 7:00 AM'
         '0 9 * * 1-5' -> 'Weekdays at 9:00 AM'
         '0 9 * * 1'   -> 'Mondays at 9:00 AM'
         '0 */4 * * *' -> 'Every 4 hours'
    """
    if not cron or not isinstance(cron, str):
        return cron or ""
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron

    minute_s, hour_s, dom, month, dow = parts

    def format_12h_time(h: int, m: int) -> str:
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12
        if h12 == 0:
            h12 = 12
        return f"{h12}:{m:02d} {suffix}"

    dow_names = {
        "0": "Sunday", "7": "Sunday",
        "1": "Monday", "2": "Tuesday", "3": "Wednesday",
        "4": "Thursday", "5": "Friday", "6": "Saturday"
    }

    # Case 1: Specific time of day (minute and hour are digits)
    if minute_s.isdigit() and hour_s.isdigit() and dom == "*" and month == "*":
        h_val = int(hour_s)
        m_val = int(minute_s)
        t_str = format_12h_time(h_val, m_val)

        if dow == "*":
            return f"Daily at {t_str}"
        elif dow in ("1-5", "1,2,3,4,5"):
            return f"Weekdays at {t_str}"
        elif dow in ("0,6", "6,0", "6-7"):
            return f"Weekends at {t_str}"
        elif dow in dow_names:
            return f"{dow_names[dow]}s at {t_str}"
        elif "-" in dow:
            d_parts = dow.split("-")
            if len(d_parts) == 2 and d_parts[0] in dow_names and d_parts[1] in dow_names:
                return f"{dow_names[d_parts[0]]}–{dow_names[d_parts[1]]} at {t_str}"

    # Case 2: Every N hours
    if hour_s.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        try:
            n = int(hour_s[2:])
            if n == 1:
                return "Every hour" if minute_s == "0" else f"Hourly at :{int(minute_s):02d}"
            return f"Every {n} hours"
        except ValueError:
            pass

    # Case 3: Hourly at 0
    if hour_s == "*" and minute_s == "0" and dom == "*" and month == "*" and dow == "*":
        return "Every hour"

    # Case 4: Every N minutes
    if hour_s == "*" and minute_s.startswith("*/") and dom == "*" and month == "*" and dow == "*":
        try:
            n = int(minute_s[2:])
            return f"Every {n} minutes"
        except ValueError:
            pass

    return cron


def cron_to_short_description(cron: str) -> str:
    """Backwards-compatible wrapper calling format_cron_human_friendly."""
    return format_cron_human_friendly(cron)

