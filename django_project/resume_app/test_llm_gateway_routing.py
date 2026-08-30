"""Tests for LLM gateway remote-first vs local-first candidate ordering."""
from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase


def _cand(provider: str, model: str, *, priority: int, is_local: bool, preference_id: int) -> dict:
    return {
        "provider": provider,
        "model_get_llm": model,
        "model_key": model,
        "priority": priority,
        "is_local": is_local,
        "preference_id": preference_id,
        "config": None,
        "api_key": "k",
    }


class OrderedEligibleCandidatesTests(SimpleTestCase):
    @patch("resume_app.llm.gateway._get_pin", return_value=(None, None))
    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_prefer_local_false_puts_remote_before_higher_priority_ollama(
        self, mock_prefs, _cd, _pin
    ):
        from resume_app.llm.gateway import _ordered_eligible_candidates

        # Ollama is higher priority (lower number) than Groq — previously won Writer.
        mock_prefs.return_value = [
            _cand("Ollama Local", "llama3", priority=0, is_local=True, preference_id=1),
            _cand("groq", "llama-3.3-70b", priority=1, is_local=False, preference_id=2),
        ]
        ordered = _ordered_eligible_candidates(user=object(), job_cache_key="1", prefer_local=False)
        self.assertEqual(ordered[0]["provider"], "groq")
        self.assertEqual(ordered[1]["provider"], "Ollama Local")

    @patch("resume_app.llm.gateway._get_pin", return_value=(None, None))
    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_prefer_local_true_puts_ollama_first(self, mock_prefs, _cd, _pin):
        from resume_app.llm.gateway import _ordered_eligible_candidates

        mock_prefs.return_value = [
            _cand("groq", "llama-3.3-70b", priority=0, is_local=False, preference_id=2),
            _cand("Ollama Local", "llama3", priority=1, is_local=True, preference_id=1),
        ]
        ordered = _ordered_eligible_candidates(user=object(), job_cache_key="1", prefer_local=True)
        self.assertEqual(ordered[0]["provider"], "Ollama Local")
        self.assertEqual(ordered[1]["provider"], "groq")

    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_local_pin_ignored_when_remote_first_and_remotes_exist(self, mock_prefs, _cd):
        from resume_app.llm.gateway import _ordered_eligible_candidates

        mock_prefs.return_value = [
            _cand("Ollama Local", "llama3", priority=0, is_local=True, preference_id=1),
            _cand("groq", "llama-3.3-70b", priority=1, is_local=False, preference_id=2),
        ]
        with patch("resume_app.llm.gateway._get_pin", return_value=("Ollama Local", "llama3")):
            ordered = _ordered_eligible_candidates(
                user=object(), job_cache_key="1", prefer_local=False
            )
        self.assertEqual(ordered[0]["provider"], "groq")

    @patch("resume_app.llm.gateway._get_pin", return_value=(None, None))
    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_allow_local_false_excludes_ollama_local(self, mock_prefs, _cd, _pin):
        from resume_app.llm.gateway import _ordered_eligible_candidates

        mock_prefs.return_value = [
            _cand("Ollama Local", "nemotron", priority=0, is_local=True, preference_id=1),
            _cand("Ollama Cloud", "gpt-oss:120b", priority=1, is_local=False, preference_id=2),
        ]
        ordered = _ordered_eligible_candidates(
            user=object(), job_cache_key="1", prefer_local=False, allow_local=False
        )
        self.assertEqual([c["provider"] for c in ordered], ["Ollama Cloud"])

    def test_provider_is_local_forces_ollama_local_name(self):
        from resume_app.llm.gateway import provider_is_local

        self.assertTrue(provider_is_local("Ollama Local", preference_is_local=False))
        self.assertFalse(provider_is_local("Ollama Cloud", preference_is_local=False))
        self.assertTrue(provider_is_local("Ollama Cloud", preference_is_local=True))
