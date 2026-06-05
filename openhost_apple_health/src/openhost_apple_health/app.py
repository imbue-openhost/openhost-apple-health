import asyncio
import gzip
import logging
import os
import uuid
from pathlib import Path

from litestar import Litestar, Request, get, post
from litestar.response import Response

from . import db
from .ingest import ingest_payload
from .manual_import import process_import
from .service import (
    service_list_metrics,
    service_get_time_series,
    service_get_sleep_sessions,
    service_get_workouts,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Uploaded files are streamed here (same volume as the DB) then deleted.
UPLOAD_DIR = os.path.dirname(os.path.abspath(db.DB_PATH)) or "."
UPLOAD_CHUNK_FIELDS = (
    "id", "filename", "status", "total_workouts", "processed_workouts",
    "total_metrics", "processed_metrics", "error", "created_at", "updated_at",
)


@get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@post("/api/data")
async def ingest_data(request: Request) -> Response:
    token = request.headers.get("api-key", "")
    if token != await db.ensure_write_token():
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


@post("/api/import", request_max_body_size=None)
async def import_upload(request: Request) -> Response:
    """Stream a Health Auto Export (zip or plain JSON) to disk and process it in the background.

    Owner-only: this route is not in the manifest's public_paths, so the router
    requires the compute space owner to be logged in. The export format is
    detected from content, not the filename.
    """
    job_id = uuid.uuid4().hex
    filename = request.headers.get("x-filename") or "upload"
    data_path = os.path.join(UPLOAD_DIR, f"import-{job_id}.data")

    size = 0
    try:
        with open(data_path, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
                size += len(chunk)
    except Exception:
        log.exception("Upload failed for job %s", job_id)
        try:
            os.remove(data_path)
        except OSError:
            pass
        return Response(content={"error": "Upload failed"}, status_code=500)

    log.info("Received manual import %s (%s, %d bytes)", job_id, filename, size)
    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO import_jobs (id, filename, status) VALUES (?, ?, 'processing')",
            (job_id, filename),
        )
        await conn.commit()

    asyncio.create_task(process_import(job_id, data_path))
    return Response(content={"job_id": job_id}, status_code=202)


@get("/api/import/{job_id:str}")
async def import_status(job_id: str) -> Response:
    cols = ", ".join(UPLOAD_CHUNK_FIELDS)
    async with db.connect() as conn:
        row = await (await conn.execute(
            f"SELECT {cols} FROM import_jobs WHERE id = ?", (job_id,)
        )).fetchone()
    if not row:
        return Response(content={"error": "not found"}, status_code=404)
    return Response(content=dict(row))


@get("/workouts/{workout_id:str}/route.gpx")
async def workout_route(workout_id: str) -> Response:
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT gpx_gzip FROM workout_routes WHERE workout_id = ?", (workout_id,)
        )).fetchone()
    if not row:
        return Response(content={"error": "not found"}, status_code=404)
    return Response(content=gzip.decompress(row[0]), media_type="application/gpx+xml")


@get("/")
async def index() -> Response:
    return Response(content=DASHBOARD_HTML, media_type="text/html")


@get("/settings")
async def settings_page() -> Response:
    return Response(content=SETTINGS_HTML, media_type="text/html")


@get("/api/v1/settings")
async def get_settings() -> dict:
    app_name = os.environ.get("OPENHOST_APP_NAME", "apple-health")
    zone = os.environ.get("OPENHOST_ZONE_DOMAIN")
    upload_url = f"https://{app_name}.{zone}/api/data" if zone else "/api/data"
    return {
        "api_key": await db.ensure_write_token(),
        "upload_url": upload_url,
    }


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
    await db.ensure_write_token()
    log.info("Database initialized, ready to receive data")


_TEMPLATES = Path(__file__).parent / "templates"
DASHBOARD_HTML = (_TEMPLATES / "dashboard.html").read_text()
SETTINGS_HTML = (_TEMPLATES / "settings.html").read_text()


app = Litestar(
    route_handlers=[
        health_check, index, settings_page, get_settings, ingest_data,
        import_upload, import_status, workout_route,
        get_heart_rate, get_stats,
        service_list_metrics, service_get_time_series,
        service_get_sleep_sessions, service_get_workouts,
    ],
    on_startup=[on_startup],
    request_max_body_size=100 * 1024 * 1024,
)
