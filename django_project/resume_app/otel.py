"""
OpenTelemetry (OTel) observability integration for HireEdge.
Vendor-neutral distributed tracing for Django HTTP endpoints, database queries, and Huey background tasks.
Exports via standard OTLP (HTTP/gRPC) to any APM backend (SigNoz, Grafana Tempo, Datadog, Honeycomb, etc.).
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

_OTEL_INITIALIZED = False


def init_opentelemetry() -> bool:
    """
    Initialize OpenTelemetry instrumentation if enabled in Django settings.
    Returns True if initialized, False if disabled or skipped.
    """
    global _OTEL_INITIALIZED
    if _OTEL_INITIALIZED:
        return True

    enabled = getattr(settings, "OTEL_ENABLED", False)
    endpoint = getattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()

    if not enabled or not endpoint:
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = getattr(settings, "OTEL_SERVICE_NAME", "hireedge")
        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)

        headers_str = getattr(settings, "OTEL_EXPORTER_OTLP_HEADERS", "").strip()
        headers = {}
        if headers_str:
            for pair in headers_str.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()

        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=headers if headers else None,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        # Instrument Django if instrumentor is available
        try:
            from opentelemetry.instrumentation.django import DjangoInstrumentor

            DjangoInstrumentor().instrument()
        except ImportError:
            logger.info("opentelemetry-instrumentation-django not installed; skipping automatic Django view instrumentation.")

        _OTEL_INITIALIZED = True
        logger.info("OpenTelemetry initialized with endpoint: %s (service: %s)", endpoint, service_name)
        return True

    except ImportError as e:
        logger.warning(
            "OTEL_ENABLED=True but OpenTelemetry packages are missing: %s. "
            "To activate, install: pip install opentelemetry-sdk opentelemetry-exporter-otlp opentelemetry-instrumentation-django",
            e,
        )
        return False
    except Exception as e:
        logger.warning("Failed to initialize OpenTelemetry: %s", e)
        return False


def get_current_trace_context() -> dict[str, str]:
    """
    Retrieve current OpenTelemetry trace_id and span_id for correlation with
    Langfuse, LiteLLM, or structured logs. Returns empty dict if no active span.
    """
    if not getattr(settings, "OTEL_ENABLED", False):
        return {}
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if not span or not span.get_span_context().is_valid:
            return {}
        ctx = span.get_span_context()
        return {
            "trace_id": format(ctx.trace_id, "032x"),
            "span_id": format(ctx.span_id, "016x"),
        }
    except Exception:
        return {}
