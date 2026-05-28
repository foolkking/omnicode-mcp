"""Monitoring / observability endpoints.

* ``GET /monitoring/metrics?format=prometheus`` — Prometheus text format
* ``GET /monitoring/metrics?format=json`` — JSON shape (default)

These endpoints expose the in-process MetricsRegistry. Counters
emitted from ``/patch/apply`` (and any future instrumented endpoint)
show up here. No external dependencies — we render the Prometheus
text format ourselves.

There's no auth on /monitoring/metrics; gate it at the reverse proxy
when you don't want metrics public. That's the same posture nginx
takes for ``/metrics`` on most stacks.
"""

from fastapi import APIRouter, Query, Response

from omnicode_core.observability import get_metrics_registry
from utils import create_success_response

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


@router.get("/metrics")
async def metrics(
    format: str = Query(  # noqa: A002 — public field name
        default="json",
        description="Output format: 'json' (default) or 'prometheus'",
    ),
):
    """Render the metrics registry.

    JSON shape::

        {
          "counters":   {"name": [{"labels": {...}, "value": N}, ...]},
          "histograms": {"name": [{"labels": {...}, "count": N,
                                   "sum": S, "buckets": {...}}, ...]}
        }

    Prometheus text format follows the standard convention used by
    ``prometheus_client``.
    """
    registry = get_metrics_registry()
    fmt = (format or "json").lower().strip()

    if fmt == "prometheus":
        body = registry.render_prometheus()
        return Response(content=body, media_type="text/plain; version=0.0.4")

    return create_success_response(registry.render_json())


__all__ = ["router"]
