"""
iCIMS adapter (icims.com).

iCIMS forms are iframe-heavy and vary widely by tenant, so this adapter is
minimal and low-confidence; semi-auto review is strongly recommended.
"""
from __future__ import annotations

from .base_form import FormAdapter


class IcimsAdapter(FormAdapter):
    ats_id = "icims"
    host_markers = ("icims.com",)
    field_selectors = {
        "first_name": ["input[name*='firstname' i]", "#firstname"],
        "last_name": ["input[name*='lastname' i]", "#lastname"],
        "email": ["input[name*='email' i]", "input[type='email']"],
        "phone": ["input[name*='phone' i]", "input[type='tel']"],
    }
    required_fields = ("first_name", "last_name", "email")
    resume_input_selectors = (
        "input[type='file']",
    )
    submit_selectors = (
        "button:has-text('Submit')",
        "input[type='submit']",
        "button[type='submit']",
    )
    confirmation_text_markers = (
        "thank you",
        "application received",
        "successfully submitted",
    )
