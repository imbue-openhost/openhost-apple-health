"""Health data service API endpoints.

Implements the provider side of the health-data service spec
(github.com/zack/services/health-data). These endpoints are served
under /api/ and consumed by other OpenHost apps via the service mesh.
"""

import logging

from litestar import Request, get

from . import db

log = logging.getLogger(__name__)

WORKOUT_TYPE_MAP = {
    "Running": "running",
    "Cycling": "cycling",
    "Swimming": "swimming",
    "Walking": "walking",
    "Hiking": "hiking",
    "Strength Training": "strength",
    "Yoga": "yoga",
    "Traditional Strength Training": "strength",
    "Functional Strength Training": "strength",
}


@get("/api/v1/metrics")
async def service_list_metrics() -> dict:
    metrics = []

    async with db.connect() as conn:
        hr_exists = await (await conn.execute(
            "SELECT 1 FROM heart_rate LIMIT 1"
        )).fetchone()
        if hr_exists:
            metrics.append({
                "metric_id": "heart_rate",
                "display_name": "Heart Rate",
                "kind": "time_series",
                "unit": "bpm",
            })

        quantity_names = await (await conn.execute(
            "SELECT DISTINCT metric_name FROM metrics ORDER BY metric_name"
        )).fetchall()
        for row in quantity_names:
            name = row[0]
            unit_row = await (await conn.execute(
                "SELECT units FROM metrics WHERE metric_name = ? LIMIT 1", (name,)
            )).fetchone()
            metrics.append({
                "metric_id": name,
                "display_name": name.replace("_", " ").title(),
                "kind": "time_series",
                "unit": unit_row[0] if unit_row else None,
            })

        sleep_exists = await (await conn.execute(
            "SELECT 1 FROM sleep_analysis LIMIT 1"
        )).fetchone()
        if sleep_exists:
            metrics.append({
                "metric_id": "sleep_analysis",
                "display_name": "Sleep Analysis",
                "kind": "time_series",
                "unit": None,
            })

    return {"metrics": metrics}


@get("/api/v1/time-series")
async def service_get_time_series(request: Request) -> dict:
    metric = request.query_params.get("metric")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "5000"))

    if not metric:
        return {"error": "metric parameter required"}

    if metric == "heart_rate":
        return await _hr_time_series(start, end, limit)

    return await _quantity_time_series(metric, start, end, limit)


async def _hr_time_series(start, end, limit) -> dict:
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
            f"SELECT date, avg_hr FROM heart_rate {where} ORDER BY date LIMIT ?",
            params,
        )).fetchall()

    return {
        "metric_id": "heart_rate",
        "display_name": "Heart Rate",
        "unit": "bpm",
        "source": "apple_health",
        "samples": [
            {"timestamp": r[0], "value": r[1]}
            for r in rows
        ],
    }


async def _quantity_time_series(metric, start, end, limit) -> dict:
    conditions = ["metric_name = ?"]
    params: list = [metric]
    if start:
        conditions.append("date >= ?")
        params.append(start)
    if end:
        conditions.append("date <= ?")
        params.append(end)
    params.append(limit)

    where = " AND ".join(conditions)
    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"SELECT date, qty, units FROM metrics WHERE {where} ORDER BY date LIMIT ?",
            params,
        )).fetchall()

    unit = rows[0][2] if rows else None
    return {
        "metric_id": metric,
        "display_name": metric.replace("_", " ").title(),
        "unit": unit,
        "source": "apple_health",
        "samples": [
            {"timestamp": r[0], "value": r[1]}
            for r in rows
        ],
    }


@get("/api/v1/sleep-sessions")
async def service_get_sleep_sessions(request: Request) -> dict:
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))

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
            f"""SELECT date, in_bed_start, in_bed_end, sleep_start, sleep_end,
                       core, rem, deep, awake, in_bed, source
                FROM sleep_analysis {where} ORDER BY date DESC LIMIT ?""",
            params,
        )).fetchall()

    def _scalar(metric_id, display_name, unit, value, source):
        return {
            "metric_id": metric_id,
            "display_name": display_name,
            "unit": unit,
            "value": value,
            "source": source,
        }

    sessions = []
    for r in reversed(rows):
        source = r[10] or "apple_health"
        session = {
            "start": r[3] or r[1],
            "end": r[4] or r[2],
            "source": source,
        }
        if r[5] is not None and r[6] is not None and r[7] is not None:
            total = r[5] + r[6] + r[7]
            session["total_duration"] = _scalar("duration", "Duration", "min", total, source)
            session["deep_sleep_duration"] = _scalar("duration", "Deep Sleep", "min", r[7], source)
            session["light_sleep_duration"] = _scalar("duration", "Light Sleep", "min", r[5], source)
            session["rem_sleep_duration"] = _scalar("duration", "REM Sleep", "min", r[6], source)
        if r[8] is not None:
            session["awake_time"] = _scalar("duration", "Awake Time", "min", r[8], source)
        if r[9] is not None:
            session["time_in_bed"] = _scalar("duration", "Time in Bed", "min", r[9], source)
        sessions.append(session)

    return {"count": len(sessions), "data": sessions}


@get("/api/v1/workouts")
async def service_get_workouts(request: Request) -> dict:
    workout_type = request.query_params.get("workout_type")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))

    conditions = []
    params: list = []
    if workout_type:
        names = [k for k, v in WORKOUT_TYPE_MAP.items() if v == workout_type]
        if names:
            placeholders = ",".join("?" * len(names))
            conditions.append(f"name IN ({placeholders})")
            params.extend(names)
        else:
            conditions.append("name = ?")
            params.append(workout_type)
    if start:
        conditions.append("start_ts >= ?")
        params.append(start)
    if end:
        conditions.append("start_ts <= ?")
        params.append(end)
    params.append(limit)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"""SELECT workout_id, name, start_ts, end_ts, duration,
                       active_energy_qty, active_energy_units,
                       distance_qty, distance_units
                FROM workouts {where} ORDER BY start_ts DESC LIMIT ?""",
            params,
        )).fetchall()

    def _scalar(metric_id, display_name, unit, value, source):
        return {
            "metric_id": metric_id,
            "display_name": display_name,
            "unit": unit,
            "value": value,
            "source": source,
        }

    workouts = []
    for r in reversed(rows):
        wtype = WORKOUT_TYPE_MAP.get(r[1], "other")
        w = {
            "start": r[2],
            "end": r[3],
            "workout_type": wtype,
            "source": "apple_health",
            "id": r[0],
        }

        metrics = {}
        if r[4] is not None:
            w["duration"] = _scalar("duration", "Duration", "s", r[4], "apple_health")
            metrics["duration_s"] = r[4]
        if r[5] is not None:
            w["calories"] = _scalar("calories", "Calories", r[6] or "kcal", r[5], "apple_health")
            metrics["calories"] = r[5]
        if r[7] is not None:
            distance_m = r[7] * 1000 if r[8] == "km" else r[7]
            metrics["distance_m"] = distance_m
        w["metrics"] = metrics

        workouts.append(w)

    return {"count": len(workouts), "data": workouts}
