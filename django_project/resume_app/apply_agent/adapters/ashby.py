"""
Ashby adapter (jobs.ashbyhq.com).

Ashby is a single-page React app with label-driven fields, so selectors are
best-effort and confidence is intentionally lower than Greenhouse/Lever. Third
wave; semi-auto review is strongly recommended for this adapter.
"""
from __future__ import annotations

from .base_form import FormAdapter


class AshbyAdapter(FormAdapter):
    ats_id = "ashby"
    host_markers = ("ashbyhq.com",)
    field_selectors = {
        "full_name": ["input[name='_systemfield_name']", "input[aria-label*='Name' i]"],
        "email": ["input[name='_systemfield_email']", "input[type='email']"],
        "phone": ["input[name='_systemfield_phone']", "input[type='tel']"],
        "linkedin": ["input[aria-label*='LinkedIn' i]", "input[name*='linkedin' i]"],
    }
    required_fields = ("full_name", "email")
    resume_input_selectors = (
        "input[type='file']",
    )
    submit_selectors = (
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "button[type='submit']",
    )
    confirmation_text_markers = (
        "thank you",
        "application submitted",
        "successfully submitted",
    )
    submit_network_markers = ("ashbyhq.com/api", "/applicationform")
