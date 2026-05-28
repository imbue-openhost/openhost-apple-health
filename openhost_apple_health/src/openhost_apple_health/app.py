import logging
import os
from pathlib import Path

from litestar import Litestar, Request, get, post
from litestar.response import Response

from . import db
from .ingest import ingest_payload
from .service import (
    service_list_metrics,
    service_get_time_series,
    service_get_sleep_sessions,
    service_get_workouts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

WRITE_TOKEN = os.environ.get("HAE_WRITE_TOKEN", "sk-hae-a7x9mQ3vR2p")


@get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@post("/api/data")
async def ingest_data(request: Request) -> Response:
    token = request.headers.get("api-key", "")
    if token != WRITE_TOKEN:
        log.warning("Unauthorized ingest attempt from %s", request.client.host if request.client else "unknown")
        return Response(content={"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        log.error("Failed to parse request body as JSON (content-type: %s, content-length: %s)",
                  request.headers.get("content-type"), request.headers.get("content-length"))
        return Response(content={"error": "Invalid JSON"}, status_code=400)

    data = body.get("data", {})
    metrics_list = data.get("metrics") or []
    workouts_list = data.get("workouts") or []
    metric_names = [m.get("name", "?") for m in metrics_list]
    total_points = sum(len(m.get("data", [])) for m in metrics_list)
    log.info("Ingest request: %d metric type(s) (%s), %d data point(s), %d workout(s)",
             len(metrics_list), ", ".join(metric_names) if metric_names else "none",
             total_points, len(workouts_list))

    try:
        result = await ingest_payload(body)
        has_errors = any(
            not v.get("success", True) for v in result.values() if isinstance(v, dict)
        )
        status = 207 if has_errors else 200

        parts = []
        for key, val in result.items():
            if isinstance(val, dict):
                saved = val.get("saved", 0)
                parts.append(f"{key}: {saved} saved")
                if val.get("errors"):
                    parts.append(f"{key} errors: {val['errors']}")
        log.info("Ingest result [%d]: %s", status, "; ".join(parts) if parts else "empty")

        return Response(content=result, status_code=status)
    except Exception:
        log.exception("Ingest failed")
        return Response(
            content={"error": "Failed to process request"},
            status_code=500,
        )


@get("/")
async def index() -> Response:
    return Response(content=DASHBOARD_HTML, media_type="text/html")


@get("/api/v1/heart-rate")
async def get_heart_rate(request: Request) -> dict:
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "5000"))

    conditions = []
    params: list = []
    if start:
        conditions.append("date >= ?")
        params.append(start)
    if end:
        conditions.append("date <= ?")
        params.append(end)
    params.append(limit)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"SELECT date, min_hr, avg_hr, max_hr FROM heart_rate {where} ORDER BY date DESC LIMIT ?",
            params,
        )).fetchall()

    return {
        "count": len(rows),
        "data": [
            {"date": r[0], "min": r[1], "avg": r[2], "max": r[3]}
            for r in reversed(rows)
        ],
    }




@get("/api/v1/stats")
async def get_stats() -> dict:
    async with db.connect() as conn:
        payload_count = (await (await conn.execute(
            "SELECT COUNT(*) FROM raw_payloads"
        )).fetchone())[0]

        metric_count = (await (await conn.execute(
            "SELECT COUNT(*) FROM metrics"
        )).fetchone())[0]

        hr_count = (await (await conn.execute(
            "SELECT COUNT(*) FROM heart_rate"
        )).fetchone())[0]

        sleep_count = (await (await conn.execute(
            "SELECT COUNT(*) FROM sleep_analysis"
        )).fetchone())[0]

        workout_count = (await (await conn.execute(
            "SELECT COUNT(*) FROM workouts"
        )).fetchone())[0]

        last_payload = (await (await conn.execute(
            "SELECT received_at FROM raw_payloads ORDER BY id DESC LIMIT 1"
        )).fetchone())

    return {
        "payloads_received": payload_count,
        "metric_samples": metric_count,
        "heart_rate_samples": hr_count,
        "sleep_sessions": sleep_count,
        "workouts": workout_count,
        "last_received": last_payload[0] if last_payload else None,
    }


async def on_startup() -> None:
    await db.init_db()
    log.info("Database initialized, ready to receive data")


_TEMPLATES = Path(__file__).parent / "templates"
DASHBOARD_HTML = (_TEMPLATES / "dashboard.html").read_text()


app = Litestar(
    route_handlers=[
        health_check, index, ingest_data,
        get_heart_rate, get_stats,
        service_list_metrics, service_get_time_series,
        service_get_sleep_sessions, service_get_workouts,
    ],
    on_startup=[on_startup],
    request_max_body_size=100 * 1024 * 1024,
)
