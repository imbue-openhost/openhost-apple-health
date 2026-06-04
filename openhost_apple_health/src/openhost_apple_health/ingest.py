import json
import logging
from datetime import datetime

from . import db
from .manual_import import route_to_gpx

log = logging.getLogger(__name__)

HEART_RATE = "heart_rate"
BLOOD_PRESSURE = "blood_pressure"
SLEEP_ANALYSIS = "sleep_analysis"

WORKOUT_CHILD_TABLES = (
    "workout_heart_rate",
    "workout_scalars",
    "workout_time_series",
    "workout_routes",
)


def _normalize_ts(ts: str | None) -> str | None:
    """Normalize a timestamp string to ISO 8601 format.

    Handles the HAE format "2026-05-27 08:16:53 -0700" and converts
    it to "2026-05-27T08:16:53-07:00" so consumers can parse it.
    """
    if not ts:
        return ts
    try:
        return datetime.fromisoformat(ts).isoformat()
    except ValueError:
        pass
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z").isoformat()
    except ValueError:
        return ts


async def ingest_payload(payload: dict) -> dict:
    raw_json = json.dumps(payload)
    data = payload.get("data", {})
    metrics_list = data.get("metrics") or []
    workouts_list = data.get("workouts") or []

    async with db.connect() as conn:
        await conn.execute(
            "INSERT INTO raw_payloads (payload, metrics_count, workouts_count) VALUES (?, ?, ?)",
            (raw_json, len(metrics_list), len(workouts_list)),
        )
        await conn.commit()

    result = {}

    if metrics_list:
        result["metrics"] = await _save_metrics(metrics_list)

    if workouts_list:
        result["workouts"] = await _save_workouts(workouts_list)

    return result


async def _save_metrics(metrics_list: list[dict]) -> dict:
    saved = 0
    errors = []

    async with db.connect() as conn:
        for metric_group in metrics_list:
            name = metric_group.get("name", "")
            try:
                saved += await save_metric_group(conn, metric_group)
            except Exception as e:
                log.exception("Error saving metric %s", name)
                errors.append(f"{name}: {e}")

        await conn.commit()

    result = {"success": not errors, "saved": saved}
    if errors:
        result["errors"] = errors
    return result


async def save_metric_group(conn, group: dict) -> int:
    """Persist one metric group; returns rows saved. Caller commits."""
    name = group.get("name", "")
    units = group.get("units", "")
    data_points = group.get("data") or []

    if name == HEART_RATE:
        return await _save_heart_rate(conn, data_points, units)
    if name == SLEEP_ANALYSIS:
        return await _save_sleep(conn, data_points, units)
    return await _save_quantity_metrics(conn, name, data_points, units)


async def _save_quantity_metrics(conn, name: str, data_points: list[dict], units: str) -> int:
    rows = []
    for dp in data_points:
        date = _normalize_ts(dp.get("date"))
        qty = dp.get("qty")
        source = dp.get("source", "")
        if date is None or qty is None:
            continue
        rows.append((name, date, qty, units, source))

    if rows:
        await conn.executemany(
            """INSERT OR REPLACE INTO metrics (metric_name, date, qty, units, source)
               VALUES (?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


async def _save_heart_rate(conn, data_points: list[dict], units: str) -> int:
    rows = []
    for dp in data_points:
        date = _normalize_ts(dp.get("date"))
        if date is None:
            continue
        min_hr = dp.get("Min")
        avg_hr = dp.get("Avg")
        max_hr = dp.get("Max")
        source = dp.get("source", "")
        if any(v is None for v in (min_hr, avg_hr, max_hr)):
            continue
        rows.append((date, min_hr, avg_hr, max_hr, units, source))

    if rows:
        await conn.executemany(
            """INSERT OR REPLACE INTO heart_rate (date, min_hr, avg_hr, max_hr, units, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


async def _save_sleep(conn, data_points: list[dict], units: str) -> int:
    rows = []
    for dp in data_points:
        date = _normalize_ts(dp.get("date"))
        if date is None:
            continue
        rows.append((
            date,
            _normalize_ts(dp.get("inBedStart")) or "",
            _normalize_ts(dp.get("inBedEnd")) or "",
            _normalize_ts(dp.get("sleepStart")) or "",
            _normalize_ts(dp.get("sleepEnd")) or "",
            dp.get("core", 0),
            dp.get("rem", 0),
            dp.get("deep", 0),
            dp.get("awake", 0),
            dp.get("inBed", 0),
            units,
            dp.get("source", ""),
        ))

    if rows:
        await conn.executemany(
            """INSERT OR REPLACE INTO sleep_analysis
               (date, in_bed_start, in_bed_end, sleep_start, sleep_end,
                core, rem, deep, awake, in_bed, units, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    return len(rows)


async def _save_workouts(workouts_list: list[dict]) -> dict:
    saved = 0
    async with db.connect() as conn:
        for w in workouts_list:
            if await save_workout(conn, w):
                saved += 1
        await conn.commit()

    return {"success": True, "saved": saved}


# HR-shaped per-minute series (items have Min/Avg/Max), stored together with a
# `series` discriminator.
_HR_SERIES = {"heartRate": "heartRateData", "heartRateRecovery": "heartRateRecovery"}


async def save_workout(conn, w: dict) -> bool:
    """Persist one workout dict and all its valuable sub-data. Caller commits.

    Captures summary scalars, per-minute series and the GPS route generically,
    so fields not known at write time are still stored rather than dropped.
    """
    workout_id = w.get("id")
    if not workout_id:
        return False

    ae = w.get("activeEnergyBurned") or {}
    dist = w.get("distance") or {}
    is_indoor = w.get("isIndoor")

    await conn.execute(
        """INSERT OR REPLACE INTO workouts
           (workout_id, name, start_ts, end_ts, duration,
            active_energy_qty, active_energy_units, distance_qty, distance_units,
            is_indoor, location)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            workout_id,
            w.get("name", ""),
            _normalize_ts(w.get("start")) or "",
            _normalize_ts(w.get("end")) or "",
            w.get("duration", 0),
            ae.get("qty"),
            ae.get("units"),
            dist.get("qty"),
            dist.get("units"),
            None if is_indoor is None else int(bool(is_indoor)),
            w.get("location"),
        ),
    )

    # Clear child rows so re-importing a workout replaces rather than duplicates.
    for table in WORKOUT_CHILD_TABLES:
        await conn.execute(f"DELETE FROM {table} WHERE workout_id = ?", (workout_id,))

    # Summary scalars: every {qty, ...} object field.
    scalar_rows = [
        (workout_id, key, val.get("qty"), val.get("units"))
        for key, val in w.items()
        if isinstance(val, dict) and "qty" in val
    ]
    # heartRate is {min, avg, max} of {qty, units}; flatten so min is captured.
    hr_summary = w.get("heartRate")
    if isinstance(hr_summary, dict):
        for src, name in (("min", "heartRateMin"), ("avg", "heartRateAvg"), ("max", "heartRateMax")):
            sub = hr_summary.get(src)
            if isinstance(sub, dict) and sub.get("qty") is not None:
                scalar_rows.append((workout_id, name, sub.get("qty"), sub.get("units")))
    if scalar_rows:
        await conn.executemany(
            "INSERT OR REPLACE INTO workout_scalars (workout_id, name, qty, units) VALUES (?, ?, ?, ?)",
            scalar_rows,
        )

    # HR-shaped series (heartRateData + heartRateRecovery).
    hr_rows = []
    for series, field in _HR_SERIES.items():
        for hr in w.get(field) or []:
            date = _normalize_ts(hr.get("date"))
            if not date:
                continue
            hr_rows.append((
                workout_id, date, hr.get("Min", 0), hr.get("Avg", 0),
                hr.get("Max", 0), hr.get("units", "bpm"), hr.get("source", ""), series,
            ))
    if hr_rows:
        await conn.executemany(
            """INSERT INTO workout_heart_rate
               (workout_id, date, min_hr, avg_hr, max_hr, units, source, series)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            hr_rows,
        )

    # Quantity per-minute series (activeEnergy, stepCount, distances, swimStroke, ...).
    ts_rows = []
    for key, val in w.items():
        if isinstance(val, list) and val and isinstance(val[0], dict) and "qty" in val[0]:
            for dp in val:
                date = _normalize_ts(dp.get("date"))
                if not date:
                    continue
                ts_rows.append((workout_id, key, date, dp.get("qty"), dp.get("units"), dp.get("source", "")))
    if ts_rows:
        await conn.executemany(
            """INSERT INTO workout_time_series (workout_id, series_name, date, qty, units, source)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ts_rows,
        )

    # GPS route -> gzipped GPX blob.
    route = w.get("route")
    if isinstance(route, list) and route:
        gpx = route_to_gpx(w.get("name", ""), route)
        await conn.execute(
            "INSERT OR REPLACE INTO workout_routes (workout_id, point_count, gpx_gzip) VALUES (?, ?, ?)",
            (workout_id, len(route), gpx),
        )

    return True
