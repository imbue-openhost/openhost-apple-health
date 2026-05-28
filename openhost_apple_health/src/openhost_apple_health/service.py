"""Health data service API endpoints.

Implements the provider side of the health-data service spec
(github.com/imbue-openhost/health-data-service-spec). These endpoints
are served under /api/ and consumed by other OpenHost apps via the
service mesh.
"""

import logging
from datetime import datetime, timezone

import attrs
from health_data_service import (
    MetricKind,
    MetricType,
    RoutePoint,
    Sample,
    SleepSession,
    TimeSeries,
    Workout,
)
from health_data_service.specific_types import Calories, Duration, HeartRate, HeartRateAvg
from litestar import Request, get

from . import db

log = logging.getLogger(__name__)

SOURCE = "apple_health"

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


def _serialize(obj):
    """Recursively convert attrs instances to dicts for JSON response."""
    if attrs.has(type(obj)):
        d = {}
        for field in attrs.fields(type(obj)):
            val = getattr(obj, field.name)
            d[field.name] = _serialize(val)
        return d
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    return obj


def _parse_ts(s: str) -> datetime:
    """Best-effort parse of various timestamp formats to datetime."""
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # HAE format: "2026-05-27 08:16:53 -0700"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


@get("/api/v1/metrics")
async def service_list_metrics() -> dict:
    metrics: list[MetricType] = []

    async with db.connect() as conn:
        hr_exists = await (await conn.execute(
            "SELECT 1 FROM heart_rate LIMIT 1"
        )).fetchone()
        if hr_exists:
            metrics.append(MetricType(
                metric_id="heart_rate",
                display_name="Heart Rate",
                kind=MetricKind.TIME_SERIES,
                unit="bpm",
            ))

        quantity_names = await (await conn.execute(
            "SELECT DISTINCT metric_name FROM metrics ORDER BY metric_name"
        )).fetchall()
        for row in quantity_names:
            name = row[0]
            unit_row = await (await conn.execute(
                "SELECT units FROM metrics WHERE metric_name = ? LIMIT 1", (name,)
            )).fetchone()
            metrics.append(MetricType(
                metric_id=name,
                display_name=name.replace("_", " ").title(),
                kind=MetricKind.TIME_SERIES,
                unit=unit_row[0] if unit_row else None,
            ))

        sleep_exists = await (await conn.execute(
            "SELECT 1 FROM sleep_analysis LIMIT 1"
        )).fetchone()
        if sleep_exists:
            metrics.append(MetricType(
                metric_id="sleep_analysis",
                display_name="Sleep Analysis",
                kind=MetricKind.TIME_SERIES,
                unit=None,
            ))

    return {"metrics": [_serialize(m) for m in metrics]}


@get("/api/v1/time-series")
async def service_get_time_series(request: Request) -> dict:
    metric = request.query_params.get("metric")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "5000"))

    if not metric:
        return {"error": "metric parameter required"}

    if metric == "heart_rate":
        ts = await _hr_time_series(start, end, limit)
    else:
        ts = await _quantity_time_series(metric, start, end, limit)

    return _serialize(ts)


async def _hr_time_series(start, end, limit) -> TimeSeries:
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT date, avg_hr FROM heart_rate ORDER BY date",
        )).fetchall()

    start_dt = _parse_ts(start) if start else None
    end_dt = _parse_ts(end) if end else None
    samples: list[Sample] = []
    for r in rows:
        ts = _parse_ts(r[0])
        if start_dt and ts < start_dt:
            continue
        if end_dt and ts > end_dt:
            continue
        samples.append(Sample(timestamp=ts, value=r[1]))
        if len(samples) >= limit:
            break

    return TimeSeries(
        metric_id="heart_rate",
        display_name="Heart Rate",
        unit="bpm",
        source=SOURCE,
        samples=samples,
    )


async def _quantity_time_series(metric, start, end, limit) -> TimeSeries:
    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT date, qty, units FROM metrics WHERE metric_name = ? ORDER BY date",
            (metric,),
        )).fetchall()

    start_dt = _parse_ts(start) if start else None
    end_dt = _parse_ts(end) if end else None
    unit = rows[0][2] if rows else None
    samples: list[Sample] = []
    for r in rows:
        ts = _parse_ts(r[0])
        if start_dt and ts < start_dt:
            continue
        if end_dt and ts > end_dt:
            continue
        samples.append(Sample(timestamp=ts, value=r[1]))
        if len(samples) >= limit:
            break

    return TimeSeries(
        metric_id=metric,
        display_name=metric.replace("_", " ").title(),
        unit=unit,
        source=SOURCE,
        samples=samples,
    )


@get("/api/v1/sleep-sessions")
async def service_get_sleep_sessions(request: Request) -> dict:
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))

    start_dt = _parse_ts(start) if start else None
    end_dt = _parse_ts(end) if end else None

    async with db.connect() as conn:
        rows = await (await conn.execute(
            "SELECT date, in_bed_start, in_bed_end, sleep_start, sleep_end,"
            "       core, rem, deep, awake, in_bed, source"
            " FROM sleep_analysis ORDER BY date DESC",
        )).fetchall()

    sessions: list[SleepSession] = []
    for r in reversed(rows):
        row_dt = _parse_ts(r[0])
        if start_dt and row_dt < start_dt:
            continue
        if end_dt and row_dt > end_dt:
            continue

        source = r[10] or SOURCE
        core, rem, deep, awake, in_bed = r[5], r[6], r[7], r[8], r[9]
        total = (core or 0) + (rem or 0) + (deep or 0)

        sessions.append(SleepSession(
            start=_parse_ts(r[3] or r[1]),
            end=_parse_ts(r[4] or r[2]),
            total_duration=Duration(value=total, source=source) if total else None,
            deep_sleep_duration=Duration(value=deep, source=source, metric_id="duration", display_name="Deep Sleep") if deep is not None else None,
            light_sleep_duration=Duration(value=core, source=source, metric_id="duration", display_name="Light Sleep") if core is not None else None,
            rem_sleep_duration=Duration(value=rem, source=source, metric_id="duration", display_name="REM Sleep") if rem is not None else None,
            awake_time=Duration(value=awake, source=source, metric_id="duration", display_name="Awake Time") if awake is not None else None,
            time_in_bed=Duration(value=in_bed, source=source, metric_id="duration", display_name="Time in Bed") if in_bed is not None else None,
            source=source,
        ))
        if len(sessions) >= limit:
            break

    return {"count": len(sessions), "data": [_serialize(s) for s in sessions]}


@get("/api/v1/workouts")
async def service_get_workouts(request: Request) -> dict:
    workout_type = request.query_params.get("workout_type")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))

    start_dt = _parse_ts(start) if start else None
    end_dt = _parse_ts(end) if end else None

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

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            f"""SELECT workout_id, name, start_ts, end_ts, duration,
                       active_energy_qty, active_energy_units,
                       distance_qty, distance_units
                FROM workouts {where} ORDER BY start_ts DESC""",
            params,
        )).fetchall()

        workouts: list[Workout] = []
        for r in reversed(rows):
            row_dt = _parse_ts(r[2])
            if start_dt and row_dt < start_dt:
                continue
            if end_dt and row_dt > end_dt:
                continue

            wtype = WORKOUT_TYPE_MAP.get(r[1], "other")
            kwargs: dict = {
                "workout_type": wtype,
                "start": row_dt,
                "end": _parse_ts(r[3]),
                "source": SOURCE,
                "id": r[0],
            }

            if r[4] is not None:
                kwargs["duration"] = Duration(value=r[4] / 60.0, source=SOURCE)
            if r[5] is not None:
                kwargs["calories"] = Calories(value=r[5], source=SOURCE)

            hr_rows = await (await conn.execute(
                "SELECT date, avg_hr FROM workout_heart_rate WHERE workout_id = ? ORDER BY date",
                (r[0],),
            )).fetchall()
            if hr_rows:
                kwargs["heart_rate"] = HeartRate(
                    source=SOURCE,
                    samples=[Sample(timestamp=_parse_ts(h[0]), value=h[1]) for h in hr_rows],
                )
                vals = [h[1] for h in hr_rows]
                kwargs["average_heart_rate"] = HeartRateAvg(value=sum(vals) / len(vals), source=SOURCE)
                kwargs["max_heart_rate"] = HeartRateAvg(
                    value=max(vals), source=SOURCE,
                    metric_id="max_heart_rate", display_name="Max Heart Rate",
                )

            route_rows = await (await conn.execute(
                "SELECT timestamp, latitude, longitude, altitude FROM workout_route WHERE workout_id = ? ORDER BY timestamp",
                (r[0],),
            )).fetchall()
            if route_rows:
                kwargs["route"] = [
                    RoutePoint(timestamp=_parse_ts(p[0]), lat=p[1], lon=p[2], altitude=p[3])
                    for p in route_rows
                ]

            workouts.append(Workout(**kwargs))
            if len(workouts) >= limit:
                break

    return {"count": len(workouts), "data": [_serialize(w) for w in workouts]}
