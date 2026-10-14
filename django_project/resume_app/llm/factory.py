from typing import List, Optional

import logging

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage, BaseMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult, ChatGeneration

from .services import DEFAULT_MODELS

logger = logging.getLogger(__name__)


class _ChatGoogleGenAINoMaxRetries(ChatGoogleGenerativeAI):
    """Workaround for langchain-google-genai 2.x: GenerativeServiceClient.generate_content() does not accept max_retries."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        kwargs.pop("max_retries", None)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    def _chat_with_retry(self, **params):
        params.pop("max_retries", None)
        return super()._chat_with_retry(**params)


class _OllamaCloudChatModel(BaseChatModel):
    """LangChain-compatible chat model for Ollama Cloud (https://ollama.com) with Bearer auth."""

    model: str
    api_key: str
    host: str = "https://ollama.com"

    @property
    def _llm_type(self) -> str:
        return "ollama_cloud"

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
        from ollama import Client

        client = Client(
            host=self.host,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        ollama_messages = self._convert_messages(messages)
        response = client.chat(model=self.model, messages=ollama_messages, stream=False)
        content = (response.get("message") or {}).get("content") or ""
        prompt_eval = response.get("prompt_eval_count")
        eval_count = response.get("eval_count")
        usage_metadata = None
        if prompt_eval is not None or eval_count is not None:
            usage_metadata = {
                "input_tokens": int(prompt_eval) if prompt_eval is not None else 0,
                "output_tokens": int(eval_count) if eval_count is not None else 0,
            }
        msg = AIMessage(content=content)
        if usage_metadata:
            msg.usage_metadata = usage_metadata
        llm_output = {}
        if usage_metadata:
            llm_output["token_usage"] = usage_metadata
        return ChatResult(
            generations=[ChatGeneration(message=msg)],
            llm_output=llm_output or None,
        )


class _OllamaLocalChatModel(BaseChatModel):
    """LangChain-compatible chat model for a locally running Ollama instance."""

    model: str
    host: str

    @property
    def _llm_type(self) -> str:
        return "ollama_local"

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

        from ollama import Client
        from .policy import invoke_timeout_seconds

        req_timeout = invoke_timeout_seconds()
        client = Client(host=self.host, timeout=req_timeout if req_timeout > 0 else None)
        ollama_messages = self._convert_messages(messages)
        total_chars = sum(len(str(m.get("content") or "")) for m in ollama_messages)
        logger.info(
            "[ollama_local] POST /api/chat host=%s model=%s messages=%s total_content_chars=%s",
            self.host,
            self.model,
            len(ollama_messages),
            total_chars,
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[ollama_local] payload preview: %s",
                [(m.get("role"), str(m.get("content") or "")[:240]) for m in ollama_messages],
            )
        t0 = time.monotonic()
        try:
            response = client.chat(model=self.model, messages=ollama_messages, stream=False)
        except Exception:
            logger.exception(
                "[ollama_local] client.chat failed after %.2fs host=%s model=%s",
                time.monotonic() - t0,
                self.host,
                self.model,
            )
            raise
        elapsed = time.monotonic() - t0
        content = (response.get("message") or {}).get("content") or ""

        # Extract Ollama timing and token evaluation telemetry
        prompt_eval_count = int(response.get("prompt_eval_count") or 0)
        prompt_eval_ns = int(response.get("prompt_eval_duration") or 0)
        prompt_eval_ms = prompt_eval_ns / 1_000_000.0 if prompt_eval_ns > 0 else 0.0

        eval_count = int(response.get("eval_count") or 0)
        eval_ns = int(response.get("eval_duration") or 0)
        eval_ms = eval_ns / 1_000_000.0 if eval_ns > 0 else 0.0

        prefill_tps = (prompt_eval_count / (prompt_eval_ms / 1000.0)) if prompt_eval_ms > 0 else 0.0
        gen_tps = (eval_count / (eval_ms / 1000.0)) if eval_ms > 0 else 0.0

        # High-throughput prompt evaluation (> 85 t/s or < 650ms on large prompts) confirms KV prefix cache reuse
        is_kv_hit = (prefill_tps > 85.0) or (prompt_eval_count > 100 and prompt_eval_ms < 650.0)
        kv_tag = " [KV CACHE HIT]" if is_kv_hit else " [COLD PREFILL]"

        logger.warning(
            "[ollama_local] %s/%s ok in %.2fs | prompt_eval=%d tok in %.0fms (%.0f t/s)%s | gen=%d tok in %.0fms (%.1f t/s)",
            self.host,
            self.model,
            elapsed,
            prompt_eval_count,
            prompt_eval_ms,
            prefill_tps,
            kv_tag,
            eval_count,
            eval_ms,
            gen_tps,
        )

        prompt_eval = response.get("prompt_eval_count")
        eval_count = response.get("eval_count")
        usage_metadata = None
        if prompt_eval is not None or eval_count is not None:
            usage_metadata = {
                "input_tokens": int(prompt_eval) if prompt_eval is not None else 0,
                "output_tokens": int(eval_count) if eval_count is not None else 0,
            }
        msg = AIMessage(content=content)
        if usage_metadata:
            msg.usage_metadata = usage_metadata
        llm_output = {}
        if usage_metadata:
            llm_output["token_usage"] = usage_metadata
        return ChatResult(
            generations=[ChatGeneration(message=msg)],
            llm_output=llm_output or None,
        )


def _normalize_ollama_local_host(host_or_ip: str, default_port: int = 11434) -> str:
    """
    Accepts:
      - "192.168.1.10" -> "http://192.168.1.10:11434"
      - "192.168.1.10:11434" -> "http://192.168.1.10:11434"
      - "http://192.168.1.10:11434" -> unchanged
    """
    import re

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


def get_llm(provider: str, api_key: str = None, model: str = None, user=None):
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
    if provider == "OpenAI":
        llm = ChatOpenAI(model=chosen, api_key=key)
    elif provider == "Anthropic":
        llm = ChatAnthropic(model=chosen, api_key=key)
    elif provider == "Groq":
        llm = ChatGroq(model=chosen, api_key=key)
    elif provider == "Google AI Studio":
        llm = _ChatGoogleGenAINoMaxRetries(model=chosen, google_api_key=key)
    elif provider == "Ollama Cloud":
        llm = _OllamaCloudChatModel(model=chosen, api_key=key)
    elif provider == "Ollama Local":
        host = _normalize_ollama_local_host(key)
        llm = _OllamaLocalChatModel(model=chosen, host=host)
    elif provider == "OpenRouter":
        ref = (getattr(settings, "OPENROUTER_HTTP_REFERER", None) or "").strip()
        headers = {"X-Title": "HireEdge"}
        if ref:
            headers["HTTP-Referer"] = ref
        llm = ChatOpenAI(
            model=chosen,
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            default_headers=headers,
        )
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    # Attach lightweight metadata for runtime failover logic and tenant ownership.
    try:
        setattr(llm, "_resume_provider", provider)
        setattr(llm, "_resume_model", chosen or None)
        if user is not None:
            setattr(llm, "_resume_user", user)
    except Exception:
        pass
    return llm

