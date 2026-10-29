"""Tests for LLM gateway remote-first vs local-first candidate ordering."""
from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase


def _cand(provider: str, model: str, *, priority: int, is_local: bool, preference_id: int, model_tier: str | None = None) -> dict:
    return {
        "provider": provider,
        "model_get_llm": model,
        "model_key": model,
        "priority": priority,
        "is_local": is_local,
        "preference_id": preference_id,
        "config": None,
        "api_key": "k",
        "model_tier": model_tier,
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

    def test_classify_model_tier(self):
        from resume_app.llm.gateway import (
            MODEL_TIER_SLM,
            MODEL_TIER_FRONTIER,
            classify_model_tier,
        )

        self.assertEqual(classify_model_tier("Ollama Local", "nemotron"), MODEL_TIER_SLM)
        self.assertEqual(classify_model_tier("Google AI Studio", "gemini-1.5-flash"), MODEL_TIER_SLM)
        self.assertEqual(classify_model_tier("Google AI Studio", "gemini-2.0-flash-lite"), MODEL_TIER_SLM)
        self.assertEqual(classify_model_tier("Google AI Studio", "gemini-1.5-pro"), MODEL_TIER_FRONTIER)
        self.assertEqual(classify_model_tier("OpenAI", "gpt-4o-mini"), MODEL_TIER_SLM)
        self.assertEqual(classify_model_tier("OpenAI", "gpt-4o"), MODEL_TIER_FRONTIER)
        self.assertEqual(classify_model_tier("Anthropic", "claude-3-5-haiku-latest"), MODEL_TIER_SLM)
        self.assertEqual(classify_model_tier("Anthropic", "claude-3-5-sonnet-latest"), MODEL_TIER_FRONTIER)
        self.assertEqual(classify_model_tier("Groq", "llama-3.1-8b-instant"), MODEL_TIER_SLM)

    @patch("resume_app.llm.gateway._get_pin", return_value=(None, None))
    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_max_model_tier_slm_filters_out_frontier_models(self, mock_prefs, _cd, _pin):
        from resume_app.llm.gateway import _ordered_eligible_candidates, MODEL_TIER_SLM

        mock_prefs.return_value = [
            _cand("Ollama Local", "nemotron", priority=0, is_local=True, preference_id=1),
            _cand("OpenAI", "gpt-4o", priority=1, is_local=False, preference_id=2),
            _cand("OpenAI", "gpt-4o-mini", priority=2, is_local=False, preference_id=3),
            _cand("Anthropic", "claude-3-5-sonnet-latest", priority=3, is_local=False, preference_id=4),
            _cand("Google AI Studio", "gemini-1.5-flash", priority=4, is_local=False, preference_id=5),
        ]
        ordered = _ordered_eligible_candidates(
            user=object(),
            job_cache_key="1",
            prefer_local=True,
            max_model_tier=MODEL_TIER_SLM,
        )
        model_names = [c["model_get_llm"] for c in ordered]
        self.assertIn("nemotron", model_names)
        self.assertIn("gpt-4o-mini", model_names)
        self.assertIn("gemini-1.5-flash", model_names)
        self.assertNotIn("gpt-4o", model_names)
        self.assertNotIn("claude-3-5-sonnet-latest", model_names)

    @patch("resume_app.llm.gateway._get_pin", return_value=(None, None))
    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_user_explicit_model_tier_overrides_heuristic(self, mock_prefs, _cd, _pin):
        from resume_app.llm.gateway import _ordered_eligible_candidates, MODEL_TIER_SLM

        mock_prefs.return_value = [
            # Custom model with no SLM keywords in name, but marked as SLM by user in UI
            _cand("OpenRouter", "custom-org/finetuned-eval", priority=1, is_local=False, preference_id=1, model_tier="slm"),
            # Model with mini in name, but explicitly locked to Frontier by user
            _cand("OpenAI", "custom-mini-reasoner", priority=2, is_local=False, preference_id=2, model_tier="frontier"),
        ]
        ordered = _ordered_eligible_candidates(
            user=object(),
            job_cache_key="1",
            prefer_local=False,
            max_model_tier=MODEL_TIER_SLM,
        )
        model_names = [c["model_get_llm"] for c in ordered]
        self.assertIn("custom-org/finetuned-eval", model_names)
        self.assertNotIn("custom-mini-reasoner", model_names)

    @patch("resume_app.llm.gateway._get_pin", return_value=(None, None))
    @patch("resume_app.llm.gateway.is_llm_on_cooldown", return_value=False)
    @patch("resume_app.llm.gateway._preference_candidates")
    def test_multiple_slms_ordered_strictly_by_priority(self, mock_prefs, _cd, _pin):
        from resume_app.llm.gateway import _ordered_eligible_candidates, MODEL_TIER_SLM

        mock_prefs.return_value = [
            _cand("Groq", "llama-3.1-8b-instant", priority=3, is_local=False, preference_id=3, model_tier="slm"),
            _cand("Google AI Studio", "gemini-2.0-flash", priority=1, is_local=False, preference_id=1, model_tier="slm"),
            _cand("OpenAI", "gpt-4o-mini", priority=2, is_local=False, preference_id=2, model_tier="slm"),
        ]
        ordered = _ordered_eligible_candidates(
            user=object(),
            job_cache_key="1",
            prefer_local=False,
            max_model_tier=MODEL_TIER_SLM,
        )
        model_names = [c["model_get_llm"] for c in ordered]
        self.assertEqual(model_names, ["gemini-2.0-flash", "gpt-4o-mini", "llama-3.1-8b-instant"])
