"""Greenhouse adapter (boards.greenhouse.io). First end-to-end target."""
from __future__ import annotations

from .base_form import FormAdapter


class GreenhouseAdapter(FormAdapter):
    ats_id = "greenhouse"
    host_markers = ("greenhouse.io", "grnhse")
    field_selectors = {
        "first_name": ["#first_name", "input[name='first_name']", "input[autocomplete='given-name']"],
        "last_name": ["#last_name", "input[name='last_name']", "input[autocomplete='family-name']"],
        "email": ["#email", "input[name='email']", "input[type='email']"],
        "phone": ["#phone", "input[name='phone']", "input[type='tel']"],
        "linkedin": ["input[name*='linkedin' i]", "input[aria-label*='LinkedIn' i]"],
    }
    required_fields = ("first_name", "last_name", "email")
    resume_input_selectors = (
        "input#resume",
        "input[type='file'][name='resume']",
        "input[type='file']",
    )
    submit_selectors = (
        "#submit_app",
        "button:has-text('Submit Application')",
        "button[type='submit']",
    )
    confirmation_text_markers = (
        "thank you for applying",
        "application submitted",
        "your application has been submitted",
    )
    submit_network_markers = ("/applications", "boards-api.greenhouse.io")
