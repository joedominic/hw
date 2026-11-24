"""Normalize and allowlist Ollama Local base URLs before the server fetches them.

User-supplied hosts are used for server-side HTTP (model list and chat). Link-local
and cloud-metadata addresses are always rejected. Public hosts are rejected unless
they match ``OLLAMA_LOCAL_HOST`` or ``OLLAMA_LOCAL_ALLOWED_HOSTS``. Loopback and
private LAN addresses stay allowed so a workstation Ollama install keeps working.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings

_METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.google.com",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


class OllamaHostRejected(ValueError):
    """The Ollama base URL is not safe to request from this process."""


def normalize_ollama_local_base_url(host_or_ip: str, default_port: int = 11434) -> str:
    """Turn a host, host:port, or URL into an allowlisted http(s) base URL.

    Raises:
        OllamaHostRejected: The URL is missing, non-http, or not on the allowlist.
    """
    raw = (host_or_ip or "").strip()
    if not raw:
        raise OllamaHostRejected("Ollama Local host/IP is required")
    if raw.startswith("http://") or raw.startswith("https://"):
        base = raw.rstrip("/")
    else:
        import re

        match = re.match(r"^(?P<host>.+):(?P<port>\d+)$", raw)
        if match:
            base = f"http://{match.group('host')}:{match.group('port')}"
        else:
            base = f"http://{raw}:{default_port}"
    return assert_ollama_local_base_allowed(base)


def assert_ollama_local_base_allowed(base_url: str) -> str:
    """Return ``base_url`` when this process may request it."""
    parsed = urlparse((base_url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise OllamaHostRejected("Ollama Local URL must use http or https")
    if parsed.username or parsed.password:
        raise OllamaHostRejected("Ollama Local URL must not include credentials")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise OllamaHostRejected("Ollama Local host is required")
    if host in _METADATA_HOSTS or host.endswith(".internal") and "metadata" in host:
        raise OllamaHostRejected("Ollama Local host is not allowed")

    if host in _allowed_hostnames():
        _reject_if_resolves_to_blocked(host, fail_closed=False)
        return base_url.rstrip("/")

    literal = _parse_ip(host)
    if literal is not None:
        if _is_blocked_ip(literal):
            raise OllamaHostRejected("Ollama Local host is not allowed")
        if literal.is_loopback or literal.is_private:
            return base_url.rstrip("/")
        raise OllamaHostRejected(
            "Ollama Local host is not allowed. Set OLLAMA_LOCAL_ALLOWED_HOSTS for a public endpoint."
        )

    addresses = _resolve(host, parsed.port or 11434)
    if not addresses:
        raise OllamaHostRejected("Ollama Local host could not be resolved")
    if any(_is_blocked_ip(ip) for ip in addresses):
        raise OllamaHostRejected("Ollama Local host is not allowed")
    if all(ip.is_loopback or ip.is_private for ip in addresses):
        return base_url.rstrip("/")
    raise OllamaHostRejected(
        "Ollama Local host is not allowed. Set OLLAMA_LOCAL_ALLOWED_HOSTS for a public endpoint."
    )


def _allowed_hostnames() -> set[str]:
    names: set[str] = set()
    configured = getattr(settings, "OLLAMA_LOCAL_ALLOWED_HOSTS", None) or []
    for item in configured:
        host = _hostname_of(str(item))
        if host:
            names.add(host)
    platform = _hostname_of(str(getattr(settings, "OLLAMA_LOCAL_HOST", "") or ""))
    if platform:
        names.add(platform)
    return names


def _hostname_of(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    host = (urlparse(raw).hostname or "").strip().lower().rstrip(".")
    return host


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (ip.version == 4 and ip in ipaddress.ip_network("169.254.0.0/16"))
    )


def _resolve(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    found: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        ip = _parse_ip(addr)
        if ip is not None:
            found.append(ip)
    return found


def _reject_if_resolves_to_blocked(host: str, *, fail_closed: bool) -> None:
    literal = _parse_ip(host)
    if literal is not None:
        if _is_blocked_ip(literal):
            raise OllamaHostRejected("Ollama Local host is not allowed")
        return
    addresses = _resolve(host, 11434)
    if not addresses:
        if fail_closed:
            raise OllamaHostRejected("Ollama Local host could not be resolved")
        return
    if any(_is_blocked_ip(ip) for ip in addresses):
        raise OllamaHostRejected("Ollama Local host is not allowed")
