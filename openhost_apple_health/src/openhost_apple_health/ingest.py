import json
import logging
from datetime import datetime, timezone

from . import db

log = logging.getLogger(__name__)

HEART_RATE = "heart_rate"
BLOOD_PRESSURE = "blood_pressure"
SLEEP_ANALYSIS = "sleep_analysis"


def _normalize_ts(ts: str | None) -> str | None:
    """Normalize a timestamp string to ISO 8601 format.

    Handles the HAE format "2026-05-27 08:16:53 -0700" and converts
    it to "2026-05-27T08:16:53-07:00".
    """
    if not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts)
        return dt.isoformat()
    except ValueError:
        pass
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
        return dt.isoformat()
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
            units = metric_group.get("units", "")
            data_points = metric_group.get("data") or []

            try:
                if name == HEART_RATE:
                    saved += await _save_heart_rate(conn, data_points, units)
                elif name == SLEEP_ANALYSIS:
                    saved += await _save_sleep(conn, data_points, units)
                else:
                    saved += await _save_quantity_metrics(conn, name, data_points, units)
            except Exception as e:
                log.exception("Error saving metric %s", name)
                errors.append(f"{name}: {e}")

        await conn.commit()

    result = {"success": not errors, "saved": saved}
    if errors:
        result["errors"] = errors
    return result


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
            workout_id = w.get("id")
            if not workout_id:
                continue

            ae = w.get("activeEnergyBurned") or {}
            dist = w.get("distance") or {}

            await conn.execute(
                """INSERT OR REPLACE INTO workouts
                   (workout_id, name, start_ts, end_ts, duration,
                    active_energy_qty, active_energy_units,
                    distance_qty, distance_units)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )

            await conn.execute(
                "DELETE FROM workout_heart_rate WHERE workout_id = ?",
                (workout_id,),
            )

            hr_data = w.get("heartRateData") or []
            hr_rows = []
            for hr in hr_data:
                date = _normalize_ts(hr.get("date"))
                if not date:
                    continue
                hr_rows.append((
                    workout_id,
                    date,
                    hr.get("Min", 0),
                    hr.get("Avg", 0),
                    hr.get("Max", 0),
                    hr.get("units", "bpm"),
                    hr.get("source", ""),
                ))

            if hr_rows:
                await conn.executemany(
                    """INSERT INTO workout_heart_rate
                       (workout_id, date, min_hr, avg_hr, max_hr, units, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    hr_rows,
                )

            await conn.execute(
                "DELETE FROM workout_route WHERE workout_id = ?",
                (workout_id,),
            )

            route_data = w.get("route") or []
            route_rows = []
            for pt in route_data:
                ts = _normalize_ts(pt.get("timestamp"))
                lat = pt.get("latitude") or pt.get("lat")
                lon = pt.get("longitude") or pt.get("lon")
                if not ts or lat is None or lon is None:
                    continue
                route_rows.append((
                    workout_id, ts, lat, lon,
                    pt.get("altitude"),
                    pt.get("speed"),
                    pt.get("course"),
                ))

            if route_rows:
                await conn.executemany(
                    """INSERT INTO workout_route
                       (workout_id, timestamp, latitude, longitude, altitude, speed, course)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    route_rows,
                )

            saved += 1

        await conn.commit()

    return {"success": True, "saved": saved}
