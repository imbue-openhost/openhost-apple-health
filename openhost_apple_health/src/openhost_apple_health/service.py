"""Health data service API endpoints.

Implements the provider side of the health-data service spec
(github.com/zack/services/health-data). These endpoints are served
under /api/ and consumed by other OpenHost apps via the service mesh.
"""

import gzip
import logging

from litestar import Request, get
from litestar.response import Response

from . import db

log = logging.getLogger(__name__)

SOURCE = "apple_health"

# Apple workout names vary ("Outdoor Run", "Pool Swim", ...); match by keyword.
# Values match the spec's WORKOUT_CLASSES keys so each type deserializes into
# the right Workout subclass on the consumer side.
WORKOUT_TYPE_PATTERNS = {
    "running": ("run",),
    "cycling": ("cycl", "bike"),
    "swimming": ("swim",),
    "walking": ("walk",),
    "hiking": ("hik",),
    "snowboarding": ("snowboard",),
    "downhill_skiing": ("ski",),
    "strength": ("strength",),
    "yoga": ("yoga",),
}

# Types whose workouts carry distance/speed/elevation (spec: DistanceWorkout).
DISTANCE_TYPES = {"running", "walking", "hiking", "cycling", "snowboarding", "downhill_skiing"}
# Foot-based types that also report pace.
PACE_TYPES = {"running", "walking", "hiking"}


def _workout_type(name: str) -> str:
    low = (name or "").lower()
    for wtype, keys in WORKOUT_TYPE_PATTERNS.items():
        if any(k in low for k in keys):
            return wtype
    return "other"


def _to_meters(qty: float, units: str | None) -> float:
    u = (units or "").lower()
    if u in ("mi", "mile", "miles"):
        return qty * 1609.344
    if u == "km":
        return qty * 1000.0
    if u in ("yd", "yard", "yards"):
        return qty * 0.9144
    if u in ("ft", "foot", "feet"):
        return qty * 0.3048
    return qty  # already meters


def _to_ms(qty: float, units: str | None) -> float:
    u = (units or "").lower()
    if u in ("mi", "mi/hr", "mph"):
        return qty * 0.44704
    if u in ("km/hr", "km/h", "kph"):
        return qty / 3.6
    return qty  # already m/s


def _f_to_c(qty: float, units: str | None = None) -> float:
    if (units or "").lower() in ("c", "degc", "°c"):
        return qty
    return (qty - 32.0) * 5.0 / 9.0


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


def _scalar(metric_id, display_name, unit, value):
    return {
        "metric_id": metric_id,
        "display_name": display_name,
        "unit": unit,
        "value": value,
        "source": SOURCE,
    }


# How to derive each spec field from a workout's stored scalars, grouped by the
# Workout subclass that owns it.
# (spec_field, scalar_name, metric_id, display_name, unit, converter)
_BASE_SCALARS = [
    ("calories", "activeEnergyBurned", "calories", "Calories", "kcal", None),
    ("intensity", "intensity", "intensity", "Intensity", "kcal/hr·kg", None),
    ("average_heart_rate", "avgHeartRate", "average_heart_rate", "Avg Heart Rate", "bpm", None),
    ("max_heart_rate", "maxHeartRate", "max_heart_rate", "Max Heart Rate", "bpm", None),
    ("lowest_heart_rate", "heartRateMin", "lowest_heart_rate", "Lowest Heart Rate", "bpm", None),
    ("temperature", "temperature", "temperature", "Temperature", "°C", _f_to_c),
    ("humidity", "humidity", "humidity", "Humidity", "%", None),
]
_DISTANCE_SCALARS = [
    ("distance", "distance", "distance", "Distance", "m", _to_meters),
    ("average_speed", "avgSpeed", "speed", "Avg Speed", "m/s", _to_ms),
    ("max_speed", "maxSpeed", "speed", "Max Speed", "m/s", _to_ms),
    ("elevation_gain", "elevationUp", "distance", "Elevation Gain", "m", _to_meters),
    ("elevation_loss", "elevationDown", "distance", "Elevation Loss", "m", _to_meters),
]
_SWIM_SCALARS = [
    ("distance", "distance", "distance", "Distance", "m", _to_meters),
    ("average_speed", "avgSpeed", "speed", "Avg Speed", "m/s", _to_ms),
    ("max_speed", "maxSpeed", "speed", "Max Speed", "m/s", _to_ms),
    ("stroke_count", "totalSwimmingStrokeCount", "stroke_count", "Stroke Count", "count", None),
    ("average_cadence", "swimCadence", "cadence", "Cadence", "count/min", None),
    ("lap_length", "lapLength", "distance", "Lap Length", "m", _to_meters),
]


def _emit_scalars(w, fields, s):
    """Add each present scalar field to workout dict w; return distance if set."""
    distance_m = None
    for field, sname, metric_id, display, unit, conv in fields:
        if sname not in s:
            continue
        qty, units = s[sname]
        if qty is None:
            continue
        value = conv(qty, units) if conv else qty
        w[field] = _scalar(metric_id, display, unit, value)
        if field == "distance":
            distance_m = value
    return distance_m


@get("/api/v1/workouts")
async def service_get_workouts(request: Request) -> dict:
    workout_type = request.query_params.get("workout_type")
    start = request.query_params.get("start")
    end = request.query_params.get("end")
    limit = int(request.query_params.get("limit", "100"))

    conditions = []
    params: list = []
    if workout_type:
        keys = WORKOUT_TYPE_PATTERNS.get(workout_type)
        if keys:
            conditions.append("(" + " OR ".join("LOWER(name) LIKE ?" for _ in keys) + ")")
            params.extend(f"%{k}%" for k in keys)
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
            f"""SELECT workout_id, name, start_ts, end_ts, duration, is_indoor
                FROM workouts {where} ORDER BY start_ts DESC LIMIT ?""",
            params,
        )).fetchall()

        ids = [r[0] for r in rows]
        scalars: dict[str, dict[str, tuple]] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            srows = await (await conn.execute(
                f"SELECT workout_id, name, qty, units FROM workout_scalars WHERE workout_id IN ({placeholders})",
                ids,
            )).fetchall()
            for wid, name, qty, units in srows:
                scalars.setdefault(wid, {})[name] = (qty, units)

    # The list returns scalar summaries only; the heart-rate trace and route
    # (potentially megabytes per workout) are served by the per-workout detail
    # endpoint below.
    workouts = [_build_workout(r, scalars.get(r[0], {})) for r in reversed(rows)]
    return {"count": len(workouts), "data": workouts}


def _build_workout(r, s, hr_samples=None, route_gpx=None) -> dict:
    """Build a workout dict from a row and its scalars. Pass hr_samples and
    route_gpx to include the full per-sample detail (used by the detail endpoint)."""
    wid, name = r[0], r[1]
    wtype = _workout_type(name)
    w = {
        "start": r[2],
        "end": r[3],
        "workout_type": wtype,
        "source": SOURCE,
        "id": wid,
    }
    if r[4] is not None:
        w["duration"] = _scalar("duration", "Duration", "min", r[4] / 60.0)
    if r[5] is not None:
        w["is_indoor"] = bool(r[5])
    if hr_samples:
        w["heart_rate"] = {
            "metric_id": "heart_rate", "display_name": "Heart Rate",
            "unit": "bpm", "source": SOURCE, "samples": hr_samples,
        }

    _emit_scalars(w, _BASE_SCALARS, s)
    distance_m = None
    if wtype == "swimming":
        distance_m = _emit_scalars(w, _SWIM_SCALARS, s)
    elif wtype in DISTANCE_TYPES:
        distance_m = _emit_scalars(w, _DISTANCE_SCALARS, s)

    # Pace, derived from distance + duration, for foot-based workouts.
    if wtype in PACE_TYPES and distance_m and r[4]:
        pace = r[4] / (distance_m / 1000.0)
        w["average_pace"] = _scalar("pace", "Avg Pace", "s/km", pace)

    # GPS route as a GPX document (route_gpx lives on the route-capable
    # subclasses: DistanceWorkout and SwimmingWorkout).
    if route_gpx and (wtype == "swimming" or wtype in DISTANCE_TYPES):
        w["route_gpx"] = route_gpx

    return w


@get("/api/v1/workouts/{workout_id:str}")
async def service_get_workout(workout_id: str) -> Response:
    """One workout's full detail: scalar metrics plus the heart-rate trace and
    GPS route that the list endpoint omits."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            """SELECT workout_id, name, start_ts, end_ts, duration, is_indoor
               FROM workouts WHERE workout_id = ?""",
            (workout_id,),
        )).fetchone()
        if not row:
            return Response(content={"error": "not found"}, status_code=404)

        srows = await (await conn.execute(
            "SELECT name, qty, units FROM workout_scalars WHERE workout_id = ?",
            (workout_id,),
        )).fetchall()
        scalars = {name: (qty, units) for name, qty, units in srows}

        hrows = await (await conn.execute(
            """SELECT date, avg_hr FROM workout_heart_rate
               WHERE series = 'heartRate' AND workout_id = ? ORDER BY date""",
            (workout_id,),
        )).fetchall()
        hr_samples = [{"timestamp": date, "value": avg} for date, avg in hrows]

        rrow = await (await conn.execute(
            "SELECT gpx_gzip FROM workout_routes WHERE workout_id = ?",
            (workout_id,),
        )).fetchone()
        route_gpx = gzip.decompress(rrow[0]).decode() if rrow else None

    return Response(content=_build_workout(row, scalars, hr_samples or None, route_gpx))
