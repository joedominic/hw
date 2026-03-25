"""Accumulate LLM token usage across graph runs via LangChain callbacks."""
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from typing import Any


class TokenUsageCallback(BaseCallbackHandler):
    """Tracks total input and output tokens from all LLM calls."""

    def __init__(self) -> None:
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        if not response:
            return
        added_in, added_out = 0, 0
        for gen_list in (response.generations or []):
            for gen in gen_list:
                if not gen or not gen.message:
                    continue
                msg = gen.message
                usage = getattr(msg, "usage_metadata", None) or {}
                if isinstance(usage, dict):
                    added_in += int(usage.get("input_tokens") or usage.get("input") or usage.get("prompt_tokens") or 0)
                    added_out += int(usage.get("output_tokens") or usage.get("output") or usage.get("completion_tokens") or 0)
        self.total_input_tokens += added_in
        self.total_output_tokens += added_out
        if added_in == 0 and added_out == 0 and getattr(response, "llm_output", None):
            tu = response.llm_output.get("token_usage") or response.llm_output.get("usage") or {}
            if isinstance(tu, dict):
                self.total_input_tokens += int(tu.get("input_tokens") or tu.get("prompt_tokens") or 0)
                self.total_output_tokens += int(tu.get("output_tokens") or tu.get("completion_tokens") or 0)
