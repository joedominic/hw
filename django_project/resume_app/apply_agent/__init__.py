"""
Autonomous Apply Agent.

Processes Applying-stage pipeline entries: optimize the resume, resolve the real
apply URL + ATS, dry-run fill the application form (capturing a semantic answer
key), pause for human approval in semi-auto, then run an atomic re-validation +
submit pass. See docs/HUEY_TASKS.md and the package modules for details.
"""
