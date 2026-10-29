"""
Tests for Langfuse telemetry naming and selective PII redaction.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase, override_settings
from langchain_core.messages import HumanMessage

from .llm.pii import mask_pii_text, mask_pii_data
from .llm.telemetry import resolve_trace_names, TRACE_NAME_MAPPINGS
from .llm.factory import LiteLLMChatModel


class PIIRedactionTests(SimpleTestCase):
    def test_redacts_email_phone_address_and_name_preserves_body(self):
        sample = (
            "# Jane Doe\n"
            "jane.doe@example.com | (555) 123-4567 | 123 Main Street, Suite 400, Seattle, WA 98101\n"
            "LinkedIn: https://linkedin.com/in/janedoe\n\n"
            "Candidate Name: Jane Doe\n"
            "Address: 123 Main Street, Seattle, WA 98101\n"
            "Phone: 555-123-4567\n"
            "Email: jane.doe@example.com\n\n"
            "Dear Hiring Team,\n"
            "I am applying for the Senior Python Engineer role at Acme Corp.\n"
            "Sincerely,\n"
            "Jane Doe\n"
        )
        masked = mask_pii_text(sample)

        # Sensitive identifiers must be replaced
        self.assertNotIn("jane.doe@example.com", masked)
        self.assertNotIn("(555) 123-4567", masked)
        self.assertNotIn("555-123-4567", masked)
        self.assertNotIn("123 Main Street", masked)
        self.assertNotIn("Seattle, WA 98101", masked)
        self.assertNotIn("janedoe", masked)

        # Placeholders present
        self.assertIn("[EMAIL]", masked)
        self.assertIn("[PHONE]", masked)
        self.assertIn("[ADDRESS]", masked)
        self.assertIn("[LINKEDIN]", masked)
        self.assertIn("[NAME]", masked)

        # Non-PII business content MUST be preserved for monitoring
        self.assertIn("Senior Python Engineer", masked)
        self.assertIn("Acme Corp", masked)
        self.assertIn("applying for the", masked)

    def test_structured_dict_masking(self):
        data = {
            "name": "Bob Builder",
            "candidate_name": "Bob Builder",
            "email": "bob@contractor.com",
            "phone": "+1 (555) 987-6543",
            "address": "789 Industrial Rd, Chicago, IL 60601",
            "ssn": "123-45-6789",
            "zip": "60601",
            "skills": ["Python", "Docker", "Kubernetes"],
            "score": 92,
            "evaluation": "Candidate Bob Builder shows strong distributed systems experience.",
        }
        masked = mask_pii_data(data)

        self.assertEqual(masked["name"], "[NAME]")
        self.assertEqual(masked["candidate_name"], "[NAME]")
        self.assertEqual(masked["email"], "[EMAIL]")
        self.assertEqual(masked["phone"], "[PHONE]")
        self.assertEqual(masked["address"], "[ADDRESS]")
        self.assertEqual(masked["ssn"], "[SSN]")
        self.assertEqual(masked["zip"], "[ZIP]")
        self.assertEqual(masked["skills"], ["Python", "Docker", "Kubernetes"])
        self.assertEqual(masked["score"], 92)
        # Evaluation text preserves non-PII and scrubs embedded name
        self.assertIn("strong distributed systems experience", masked["evaluation"])


class TelemetryNamingTests(SimpleTestCase):
    def test_optimizer_workflow_steps_grouped_under_resume_optimizer(self):
        for step in ["optimizer_writer", "optimizer_ats_judge", "optimizer_recruiter_judge", "jd_cleanse"]:
            trace_name, gen_name = resolve_trace_names(query_kind=step, job_cache_key="42")
            self.assertEqual(trace_name, "resume_optimizer")
            self.assertEqual(gen_name, step)

    def test_standalone_features_have_distinct_names(self):
        cases = {
            "fit_check": ("fit_check", "fit_check"),
            "matching": ("job_matching", "job_matching"),
            "job_insights": ("job_insights", "job_insights"),
            "cover_letter": ("cover_letter", "cover_letter"),
            "interview_prep": ("interview_prep", "interview_prep"),
            "pipeline_skill_extract": ("pipeline_skill_extract", "pipeline_skill_extract"),
        }
        for qk, expected in cases.items():
            t_name, g_name = resolve_trace_names(query_kind=qk)
            self.assertEqual((t_name, g_name), expected)

    def test_fallback_naming_never_uses_generic_litellm_completion(self):
        t_name, g_name = resolve_trace_names(
            query_kind=None,
            job_cache_key=None,
            default_name="hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M",
        )
        self.assertNotEqual(t_name, "litellm-completion")
        self.assertNotEqual(g_name, "litellm-completion")
        self.assertIn("Nemotron", t_name)


class LiteLLMChatModelTelemetryTests(SimpleTestCase):
    @patch("litellm.completion")
    @override_settings(LANGFUSE_MASK_PII=True, LANGFUSE_REDACT_CONTENT=False)
    def test_default_does_not_blank_content_and_provides_telemetry_names(self, mock_completion):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Analysis: Good fit"))]
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        mock_completion.return_value = mock_resp

        model = LiteLLMChatModel(
            model="ollama/llama3",
            metadata={"query_kind": "fit_check"},
        )
        model.invoke([HumanMessage(content="Check fit for Jane Doe, email: jane@test.com")])

        self.assertTrue(mock_completion.called)
        call_kwargs = mock_completion.call_args[1]
        metadata = call_kwargs.get("metadata") or {}

        # Content must NOT be completely wiped with mask_input/mask_output
        self.assertFalse(metadata.get("mask_input", False))
        self.assertFalse(metadata.get("mask_output", False))

        # PII masking function must be registered for LiteLLM Langfuse logger
        self.assertIn("langfuse_masking_function", metadata)
        self.assertTrue(callable(metadata["langfuse_masking_function"]))

        # Specific trace and generation names must be populated
        self.assertEqual(metadata.get("trace_name"), "fit_check")
        self.assertEqual(metadata.get("generation_name"), "fit_check")

    @patch("litellm.completion")
    @override_settings(LANGFUSE_DROP_ALL_CONTENT=True)
    def test_emergency_drop_all_content_flag(self, mock_completion):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Response"))]
        mock_resp.usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        mock_completion.return_value = mock_resp

        model = LiteLLMChatModel(model="ollama/llama3")
        model.invoke([HumanMessage(content="Test prompt")])

        call_kwargs = mock_completion.call_args[1]
        metadata = call_kwargs.get("metadata") or {}
        self.assertTrue(metadata.get("mask_input"))
        self.assertTrue(metadata.get("mask_output"))

    @patch("litellm.completion")
    @override_settings(OLLAMA_ENABLE_THINKING=False)
    def test_ollama_disables_thinking_by_default(self, mock_completion):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Hello!"))]
        mock_resp.usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        mock_completion.return_value = mock_resp

        model = LiteLLMChatModel(model="ollama/hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M")
        model.invoke([HumanMessage(content="Hello")])

        call_kwargs = mock_completion.call_args[1]
        self.assertEqual(call_kwargs.get("think"), False)

    @patch("litellm.completion")
    @override_settings(OLLAMA_ENABLE_THINKING=True)
    def test_ollama_allows_thinking_when_enabled(self, mock_completion):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="Hello!"))]
        mock_resp.usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)
        mock_completion.return_value = mock_resp

        model = LiteLLMChatModel(model="ollama/hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M")
        model.invoke([HumanMessage(content="Hello")])

        call_kwargs = mock_completion.call_args[1]
        self.assertNotIn("think", call_kwargs)

    @patch("litellm.completion")
    def test_ollama_strips_residual_think_tags_from_content(self, mock_completion):
        mock_resp = MagicMock()
        mock_resp.choices = [
            MagicMock(
                message=MagicMock(
                    content="I need to think carefully about this problem.\n</think>\nHere is the actual answer.",
                    reasoning_content=None,
                )
            )
        ]
        mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        mock_completion.return_value = mock_resp

        model = LiteLLMChatModel(model="ollama/hf.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M")
        res = model.invoke([HumanMessage(content="Question")])

        self.assertEqual(res.content, "Here is the actual answer.")
        self.assertEqual(
            res.additional_kwargs.get("reasoning_content"),
            "I need to think carefully about this problem.",
        )
