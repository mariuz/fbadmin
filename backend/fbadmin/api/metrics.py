"""Optional Prometheus exporter: ``GET /metrics`` (disabled by default).

Enable with ``FBADMIN_METRICS=1`` or in the monitoring settings. Databases scraped: those with history
tracking enabled, or all registered databases with ``?all=1``. Set ``FBADMIN_METRICS_TOKEN`` to require
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse

from ..monitoring import history, metrics, probes
from ..query import end_transaction
from .deps import get_state

router = APIRouter(tags=["metrics"])


@router.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(all: bool = False, authorization: str | None = Header(default=None)):  # noqa: A002
    store = history.get_store()
    if not store.get_settings()["metrics_enabled"]:
        raise HTTPException(status_code=404, detail="Metrics exporter is disabled (set FBADMIN_METRICS=1 or "
                                                    "enable it in Monitoring settings)")
    token = os.environ.get("FBADMIN_METRICS_TOKEN")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Bearer token required")
    st = get_state()
    targets: list[tuple[str, str]] = []
    if all:
        for srv in st.store.list():
            targets += [(srv.id, d.id) for d in srv.databases]
    else:
        targets = [(t["server_id"], t["db_id"]) for t in store.tracked()]
    samples = []
    for sid, dbid in targets:
        try:
            srv = st.store.get(sid)
            db = st.store.get_database(sid, dbid)
        except Exception:  # noqa: BLE001
            continue
        labels = {"server": srv.name, "host": f"{srv.host}:{srv.port}", "database": db.name}
        t0 = time.perf_counter()
        snap = None
        try:
            with st.manager.meta(sid, dbid) as conn:
                snap = probes.snapshot(conn)
                end_transaction(conn, commit=True)
        except Exception:  # noqa: BLE001 - reported as fbadmin_database_up 0
            snap = None
        samples.append((labels, snap, time.perf_counter() - t0))
    return PlainTextResponse(metrics.render(samples), media_type="text/plain; version=0.0.4; charset=utf-8")
