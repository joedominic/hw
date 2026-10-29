from typing import Any, List, Optional
import logging
import re
import litellm

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration

from .services import DEFAULT_MODELS

logger = logging.getLogger(__name__)

# Suppress verbose debug info from litellm in standard logs
litellm.suppress_debug_info = True


def configure_langfuse_telemetry():
    """Configure LiteLLM to export traces to Langfuse if configured in Django settings or environment."""
    import os
    from django.conf import settings

    pub_key = getattr(settings, "LANGFUSE_PUBLIC_KEY", "") or os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    sec_key = getattr(settings, "LANGFUSE_SECRET_KEY", "") or os.environ.get("LANGFUSE_SECRET_KEY", "")
    base_url = (
        getattr(settings, "LANGFUSE_BASE_URL", "")
        or getattr(settings, "LANGFUSE_HOST", "")
        or os.environ.get("LANGFUSE_BASE_URL", "")
        or os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    )
    enabled = getattr(settings, "LANGFUSE_ENABLED", None)
    if enabled is None:
        enabled = bool(pub_key and sec_key)

    if enabled and pub_key and sec_key:
        os.environ["LANGFUSE_PUBLIC_KEY"] = pub_key
        os.environ["LANGFUSE_SECRET_KEY"] = sec_key
        if base_url:
            os.environ["LANGFUSE_BASE_URL"] = base_url
            os.environ["LANGFUSE_HOST"] = base_url

        callbacks = litellm.success_callback or []
        if "langfuse" not in callbacks:
            litellm.success_callback.append("langfuse")

        fail_callbacks = litellm.failure_callback or []
        if "langfuse" not in fail_callbacks:
            litellm.failure_callback.append("langfuse")

        logger.info("[langfuse] Telemetry enabled (base_url=%s)", base_url)
    else:
        logger.debug("[langfuse] Telemetry inactive (missing keys or disabled)")


class LiteLLMChatModel(BaseChatModel):
    """LangChain-compatible chat model powered by LiteLLM."""

    model: str
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    extra_headers: Optional[dict] = None
    timeout: Optional[float] = None
    temperature: float = 0.0
    metadata: Optional[dict] = None

    @property
    def _llm_type(self) -> str:
        return "litellm"

    def _convert_messages(self, messages: List[BaseMessage]) -> List[dict]:
        out = []
        for m in messages:
            if isinstance(m, HumanMessage):
                role = "user"
            elif isinstance(m, AIMessage):
                role = "assistant"
            elif isinstance(m, SystemMessage):
                role = "system"
            else:
                role = "user"
            content = getattr(m, "content", None) or str(m)
            if isinstance(content, list):
                content = "".join(
                    c.get("text", c) if isinstance(c, dict) else str(c) for c in content
                )
            out.append({"role": role, "content": content})
        return out

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager=None,
        **kwargs,
    ) -> ChatResult:
        import time
        from django.conf import settings
        from .policy import invoke_timeout_seconds

        req_timeout = self.timeout or invoke_timeout_seconds()
        ollama_messages = self._convert_messages(messages)
        total_chars = sum(len(str(m.get("content") or "")) for m in ollama_messages)

        logger.info(
            "[litellm] invocation model=%s host=%s messages=%s total_chars=%s",
            self.model,
            self.api_base or "cloud",
            len(ollama_messages),
            total_chars,
        )

        call_metadata = dict(self.metadata or {})
        # If kwargs or run_manager pass additional metadata, merge it
        if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
            call_metadata.update(kwargs["metadata"])
        if run_manager and hasattr(run_manager, "metadata") and isinstance(run_manager.metadata, dict):
            call_metadata.update(run_manager.metadata)

        # Resolve descriptive trace_name and generation_name for Langfuse telemetry
        trace_name = call_metadata.get("trace_name")
        generation_name = call_metadata.get("generation_name")
        if not trace_name or not generation_name:
            from .telemetry import resolve_trace_names

            qk = call_metadata.get("query_kind")
            jck = call_metadata.get("job_cache_key") or call_metadata.get("session_id")
            resolved_trace, resolved_gen = resolve_trace_names(
                query_kind=qk,
                job_cache_key=jck,
                default_name=self.model,
            )
            if not trace_name:
                call_metadata["trace_name"] = resolved_trace
            if not generation_name:
                call_metadata["generation_name"] = resolved_gen

            from ..otel import get_current_trace_context
            otel_ctx = get_current_trace_context()
            if otel_ctx.get("trace_id"):
                call_metadata["otel_trace_id"] = otel_ctx["trace_id"]
                call_metadata["otel_span_id"] = otel_ctx.get("span_id", "")

        # Inject PII privacy guardrails for Langfuse telemetry
        drop_all_content = getattr(settings, "LANGFUSE_DROP_ALL_CONTENT", False) or getattr(
            settings, "LANGFUSE_REDACT_CONTENT", False
        )
        mask_pii_enabled = getattr(settings, "LANGFUSE_MASK_PII", True)

        if drop_all_content:
            call_metadata["mask_input"] = True
            call_metadata["mask_output"] = True
        elif mask_pii_enabled:
            from .pii import mask_pii_data

            call_metadata["langfuse_masking_function"] = mask_pii_data

        # For Ollama models with reasoning/thinking capabilities (e.g. Nemotron, DeepSeek-R1),
        # allow full thinking by default for accuracy unless explicitly disabled via settings or kwargs.
        extra_completion_kwargs = {}
        if "ollama" in self.model:
            enable_thinking = kwargs.get("think", getattr(settings, "OLLAMA_ENABLE_THINKING", True))
            # Skill Radar is an extraction/classification task; disable speculative thinking when supported.
            # Use max_tokens=2048 so reasoning models (e.g. gpt-oss, DeepSeek-R1) have room for both reasoning & JSON.
            if call_metadata.get("query_kind") == "skill_radar":
                enable_thinking = False
                extra_completion_kwargs["max_tokens"] = 2048
                if not stop:
                    stop = ["<|im_end|>", "<|end_of_text|>", "<|eot_id|>", "<|im_start|>"]
            if not enable_thinking:
                extra_completion_kwargs["think"] = False
                extra_completion_kwargs["reasoning_effort"] = "none"
                extra_body = dict(extra_completion_kwargs.get("extra_body") or {})
                extra_body["think"] = False
                extra_completion_kwargs["extra_body"] = extra_body

        t0 = time.monotonic()
        try:
            response = litellm.completion(
                model=self.model,
                messages=ollama_messages,
                api_key=self.api_key,
                api_base=self.api_base,
                extra_headers=self.extra_headers,
                timeout=req_timeout if req_timeout > 0 else None,
                temperature=self.temperature,
                stop=stop,
                metadata=call_metadata or None,
                **extra_completion_kwargs,
            )
        except Exception:
            logger.exception(
                "[litellm] call failed after %.2fs model=%s api_base=%s",
                time.monotonic() - t0,
                self.model,
                self.api_base,
            )
            raise

        elapsed = time.monotonic() - t0
        choice = response.choices[0]
        content = choice.message.content or ""

        # Clean reasoning/thinking artifacts if model leaked them into content
        reasoning_content = getattr(choice.message, "reasoning_content", None)
        if "</think>" in content:
            parts = content.split("</think>", 1)
            raw_thought = parts[0]
            if "<think>" in raw_thought:
                raw_thought = raw_thought.split("<think>", 1)[-1]
            if not reasoning_content:
                reasoning_content = raw_thought.strip()
            content = parts[1].strip()

        # Normalized token usage across all providers
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0

        usage_metadata = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

        # Telemetry for Ollama KV cache evaluation if available in raw response
        if "ollama" in self.model:
            prompt_eval_ns = getattr(response, "prompt_eval_duration", None)
            if isinstance(prompt_eval_ns, (int, float)) and prompt_eval_ns > 0:
                prompt_eval_ms = prompt_eval_ns / 1_000_000.0
                prefill_tps = (prompt_tokens / (prompt_eval_ms / 1000.0)) if prompt_eval_ms > 0 else 0.0
                is_kv_hit = prefill_tps > 85.0
                logger.info(
                    "[ollama_local] %s in %.2fs | %d tok (%.0f t/s)%s",
                    self.model,
                    elapsed,
                    prompt_tokens,
                    prefill_tps,
                    " [KV HIT]" if is_kv_hit else " [PREFILL]",
                )

        additional_kwargs = {}
        if reasoning_content:
            additional_kwargs["reasoning_content"] = reasoning_content

        msg = AIMessage(content=content, additional_kwargs=additional_kwargs)
        msg.usage_metadata = usage_metadata
        llm_output = {"token_usage": usage_metadata}
        return ChatResult(
            generations=[ChatGeneration(message=msg)],
            llm_output=llm_output,
        )

    def with_structured_output(self, schema: Any, **kwargs):
        from pydantic import BaseModel
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            from langchain_core.output_parsers import PydanticOutputParser
            return self | PydanticOutputParser(pydantic_object=schema)
        from langchain_core.output_parsers import JsonOutputParser
        return self | JsonOutputParser()


def _normalize_ollama_local_host(host_or_ip: str, default_port: int = 11434) -> str:
    """
    Accepts:
      - "192.168.1.10" -> "http://192.168.1.10:11434"
      - "192.168.1.10:11434" -> "http://192.168.1.10:11434"
      - "http://192.168.1.10:11434" -> unchanged
    """
    raw = (host_or_ip or "").strip()
    if not raw:
        raise ValueError("Ollama Local host/IP is required")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    m = re.match(r"^(?P<host>.+):(?P<port>\d+)$", raw)
    if m:
        host = m.group("host")
        port = m.group("port")
        return f"http://{host}:{port}"
    return f"http://{raw}:{default_port}"


def map_provider_model(provider: str, model: str) -> tuple[str, Optional[str], Optional[dict]]:
    """
    Translate internal provider name + model tag to LiteLLM model identifier and base URL.
    """
    p = (provider or "").strip()
    m = (model or "").strip()
    api_base = None
    extra_headers = None

    if p == "OpenAI":
        litellm_model = m if m.startswith("openai/") else m
    elif p == "Anthropic":
        litellm_model = m if (m.startswith("anthropic/") or m.startswith("claude")) else f"anthropic/{m}"
    elif p == "Groq":
        litellm_model = m if m.startswith("groq/") else f"groq/{m}"
    elif p == "Google AI Studio":
        litellm_model = m if m.startswith("gemini/") else f"gemini/{m}"
    elif p == "Ollama Local":
        litellm_model = m if m.startswith("ollama/") else f"ollama/{m}"
    elif p == "Ollama Cloud":
        litellm_model = m if m.startswith("ollama/") else f"ollama/{m}"
        api_base = "https://ollama.com"
    elif p == "OpenRouter":
        litellm_model = m if m.startswith("openrouter/") else f"openrouter/{m}"
    else:
        litellm_model = m

    return litellm_model, api_base, extra_headers


def get_llm(provider: str, api_key: str = None, model: str = None, user=None, metadata: dict = None):
    """
    Instantiate a unified LiteLLMChatModel for the specified provider and model.
    """
    from django.conf import settings

    key = api_key
    if not key:
        if provider == "OpenAI":
            key = getattr(settings, "OPENAI_API_KEY", None)
        elif provider == "Anthropic":
            key = getattr(settings, "ANTHROPIC_API_KEY", None)
        elif provider == "Groq":
            key = getattr(settings, "GROQ_API_KEY", None)
        elif provider == "Google AI Studio":
            key = getattr(settings, "GOOGLE_API_KEY", None)
        elif provider == "Ollama Cloud":
            key = getattr(settings, "OLLAMA_API_KEY", None)
        elif provider == "Ollama Local":
            if user:
                try:
                    from ..models import LLMProviderConfig
                    from ..crypto import decrypt_api_key

                    cfg = (
                        LLMProviderConfig.objects.for_user(user)
                        .filter(provider="Ollama Local")
                        .first()
                    )
                    if cfg and cfg.encrypted_api_key:
                        key = decrypt_api_key(cfg.encrypted_api_key)
                except Exception:
                    pass
            if not key:
                key = (
                    getattr(settings, "OLLAMA_LOCAL_HOST", None)
                    or getattr(settings, "OLLAMA_HOST", None)
                    or "http://127.0.0.1:11434"
                )
        elif provider == "OpenRouter":
            key = getattr(settings, "OPENROUTER_API_KEY", None)
        if not key and provider != "Ollama Local":
            raise ValueError(
                f"API key required for {provider}. Set in request or in env (e.g. OPENAI_API_KEY)."
            )

    chosen = model or DEFAULT_MODELS.get(provider, "")
    litellm_model, api_base, extra_headers = map_provider_model(provider, chosen)

    if provider == "Ollama Local":
        api_base = _normalize_ollama_local_host(key)
        key = None
        if api_base:
            import os
            os.environ.setdefault("OLLAMA_API_BASE", api_base)
        if litellm_model not in litellm.model_cost:
            try:
                litellm.register_model({
                    litellm_model: {
                        "max_tokens": 4096,
                        "input_cost_per_token": 0.0,
                        "output_cost_per_token": 0.0,
                        "litellm_provider": "ollama",
                        "mode": "chat",
                    }
                })
            except Exception:
                pass
    elif provider == "Ollama Cloud":
        api_base = "https://ollama.com"
    elif provider == "OpenRouter":
        ref = (getattr(settings, "OPENROUTER_HTTP_REFERER", None) or "").strip()
        headers = {"X-Title": "HireEdge"}
        if ref:
            headers["HTTP-Referer"] = ref
        extra_headers = headers

    # Base metadata for observability
    call_metadata = dict(metadata or {})
    if user is not None:
        user_id = getattr(user, "id", None) or str(user)
        call_metadata.setdefault("trace_user_id", str(user_id))
        call_metadata.setdefault("user_id", str(user_id))

    if not call_metadata.get("trace_name") or not call_metadata.get("generation_name"):
        from .telemetry import resolve_trace_names

        qk = call_metadata.get("query_kind")
        jck = call_metadata.get("job_cache_key") or call_metadata.get("session_id")
        t_name, g_name = resolve_trace_names(
            query_kind=qk,
            job_cache_key=jck,
            default_name=chosen or litellm_model,
        )
        call_metadata.setdefault("trace_name", t_name)
        call_metadata.setdefault("generation_name", g_name)

    llm = LiteLLMChatModel(
        model=litellm_model,
        api_key=key,
        api_base=api_base,
        extra_headers=extra_headers,
        metadata=call_metadata or None,
    )

    # Attach lightweight metadata for runtime failover logic and tenant ownership.
    try:
        setattr(llm, "_resume_provider", provider)
        setattr(llm, "_resume_model", chosen or None)
        if user is not None:
            setattr(llm, "_resume_user", user)
    except Exception:
        pass
    return llm
