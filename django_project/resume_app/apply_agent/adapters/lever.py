"""Lever adapter (jobs.lever.co). Uses a single full-name field."""
from __future__ import annotations

from .base_form import FormAdapter


class LeverAdapter(FormAdapter):
    ats_id = "lever"
    host_markers = ("lever.co",)
    field_selectors = {
        "full_name": ["input[name='name']", "input[autocomplete='name']"],
        "email": ["input[name='email']", "input[type='email']"],
        "phone": ["input[name='phone']", "input[type='tel']"],
        "linkedin": ["input[name='urls[LinkedIn]']", "input[name*='linkedin' i]"],
        "website": ["input[name='urls[Portfolio]']", "input[name*='portfolio' i]"],
    }
    required_fields = ("full_name", "email")
    resume_input_selectors = (
        "input[name='resume']",
        "input[type='file']",
    )
    submit_selectors = (
        "button:has-text('Submit application')",
        "button[type='submit']",
    )
    confirmation_text_markers = (
        "thank you for applying",
        "application submitted",
        "we received your application",
    )
    confirmation_url_markers = ("thanks", "confirmation", "submitted")
    submit_network_markers = ("api.lever.co", "/postings")
