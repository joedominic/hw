"""
Fetch list of available models from each LLM provider.
Used to validate API key (connection works) and to populate model dropdown.
"""
import logging
from typing import List

logger = logging.getLogger(__name__)

# Supported providers and default models
LLM_PROVIDERS = {
    "OpenAI",
    "Anthropic",
    "Groq",
    "Google AI Studio",
    "Ollama Cloud",
    "Ollama Local",
    "OpenRouter",
}

# Default models when list fails or as fallback
DEFAULT_MODELS = {
    "OpenAI": "gpt-4o",
    "Anthropic": "claude-3-5-sonnet-latest",
    "Groq": "llama3-70b-8192",
    "Google AI Studio": "gemini-1.5-pro",
    "Ollama Cloud": "gpt-oss:120b",
    "Ollama Local": "llama3",
    "OpenRouter": "openai/gpt-4o-mini",
}


def _list_models_generic(api_key: str, fetch_fn, extract_fn, provider: str) -> List[str]:
    """
    Shared wrapper: call provider SDK/http, extract ids, apply defaults and logging.
    """
    try:
        resp = fetch_fn(api_key)
        ids = extract_fn(resp) or []
        if not ids:
            return [DEFAULT_MODELS[provider]]
        return sorted(ids)[:50]
    except Exception as e:
        logger.warning("%s list_models failed: %s", provider, e)
        raise


def list_models_openai(api_key: str) -> List[str]:
    def fetch(key: str):
        from openai import OpenAI

        client = OpenAI(api_key=key)
        return client.models.list()

    def extract(resp) -> List[str]:
        data = getattr(resp, "data", []) or []
        ids = [m.id for m in data if getattr(m, "id", None)]
        # Prefer GPT-4 family first.
        ids = sorted(ids, key=lambda x: (not x.startswith("gpt-4"), x))
        return ids

    return _list_models_generic(api_key, fetch, extract, "OpenAI")


def list_models_anthropic(api_key: str) -> List[str]:
    def fetch(key: str):
        from anthropic import Anthropic

        client = Anthropic(api_key=key)
        return client.models.list()

    def extract(page) -> List[str]:
        data = getattr(page, "data", []) or []
        return [m.id for m in data if getattr(m, "id", None)]

    return _list_models_generic(api_key, fetch, extract, "Anthropic")


def list_models_groq(api_key: str) -> List[str]:
    def fetch(key: str):
        from groq import Groq

        client = Groq(api_key=key)
        return client.models.list()

    def extract(resp) -> List[str]:
        data = getattr(resp, "data", []) or []
        return [m.id for m in data if getattr(m, "id", None)]

    return _list_models_generic(api_key, fetch, extract, "Groq")


def list_models_google(api_key: str) -> List[str]:
    def fetch(key: str):
        import requests

        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": key},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def extract(data: dict) -> List[str]:
        models = data.get("models") or []
        return [
            m["name"].replace("models/", "")
            for m in models
            if "generateContent" in (m.get("supportedGenerationMethods") or [])
        ]

    return _list_models_generic(api_key, fetch, extract, "Google AI Studio")


def list_models_ollama_cloud(api_key: str) -> List[str]:
    """List models from Ollama Cloud API (https://ollama.com/api/tags)."""
    def fetch(key: str):
        import requests

        r = requests.get(
            "https://ollama.com/api/tags",
            headers={"Authorization": f"Bearer {key}"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def extract(data: dict) -> List[str]:
        models = data.get("models") or []
        ids: List[str] = []
        for m in models:
            name = m.get("name") or (m.get("model") if isinstance(m.get("model"), str) else None)
            if name:
                ids.append(name)
        return ids

    return _list_models_generic(api_key, fetch, extract, "Ollama Cloud")


def _normalize_ollama_local_base_url(host_or_ip: str, default_port: int = 11434) -> str:
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
    # If it already looks like host:port, keep that port.
    m = re.match(r"^(?P<host>.+):(?P<port>\d+)$", raw)
    if m:
        host = m.group("host")
        port = m.group("port")
        return f"http://{host}:{port}"
    return f"http://{raw}:{default_port}"


def list_models_ollama_local(host_or_ip: str) -> List[str]:
    """List models from a locally running Ollama instance (no auth)."""

    base_url = _normalize_ollama_local_base_url(host_or_ip)

    def fetch(key: str):
        import requests

        # key is our host/IP for this provider
        url = _normalize_ollama_local_base_url(key) + "/api/tags"
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()

    def extract(data: dict) -> List[str]:
        models = data.get("models") or []
        ids: List[str] = []
        for m in models:
            name = m.get("name") or (m.get("model") if isinstance(m.get("model"), str) else None)
            if name:
                ids.append(name)
        return ids

    # Use "Ollama Local" default model fallback.
    return _list_models_generic(host_or_ip, fetch, extract, "Ollama Local")


def list_models_openrouter(api_key: str) -> List[str]:
    """List models from OpenRouter (OpenAI-compatible /v1/models)."""

    def fetch(key: str):
        import requests

        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def extract(data: dict) -> List[str]:
        rows = data.get("data") or []
        ids = [m["id"] for m in rows if isinstance(m, dict) and m.get("id")]
        return ids

    return _list_models_generic(api_key, fetch, extract, "OpenRouter")


def list_models_for_provider(provider: str, api_key: str) -> List[str]:
    """Returns list of model ids. Raises on auth/invalid key."""
    if provider == "OpenAI":
        return list_models_openai(api_key)
    if provider == "Anthropic":
        return list_models_anthropic(api_key)
    if provider == "Groq":
        return list_models_groq(api_key)
    if provider == "Google AI Studio":
        return list_models_google(api_key)
    if provider == "Ollama Cloud":
        return list_models_ollama_cloud(api_key)
    if provider == "Ollama Local":
        return list_models_ollama_local(api_key)
    if provider == "OpenRouter":
        return list_models_openrouter(api_key)
    raise ValueError(f"Unknown provider: {provider}")


def is_auth_error(exc: Exception) -> bool:
    """True if exception indicates invalid/expired API key."""
    msg = (getattr(exc, "message", "") or str(exc)).lower()
    if "invalid" in msg or "api_key" in msg or "401" in msg or "403" in msg or "authentication" in msg:
        return True
    return False
