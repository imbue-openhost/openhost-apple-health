"""Manual import: process a Health Auto Export without holding it in memory.

The upload is either a plain ``HealthAutoExport-*.json`` or a zip wrapping that
JSON alongside per-workout GPX files. Either way we read only the JSON and
regenerate GPX from each workout's ``route`` field (a strict superset of the
bundled GPX). The JSON is stream-parsed with ijson so a 600MB file fits in a
256-512MB container.
"""

import asyncio
import gzip
import logging
import os
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

import ijson

from . import db

log = logging.getLogger(__name__)

BATCH_COMMIT = 25


def _to_utc_z(ts: str) -> str:
    """Convert '2026-06-01 16:46:35 -0700' to GPX UTC '2026-06-01T23:46:35Z'."""
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S %z")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route_to_gpx(name: str, points: list[dict]) -> bytes:
    """Build a GPX 1.1 document from JSON route points and gzip it."""
    first_time = ""
    for p in points:
        if p.get("timestamp"):
            try:
                first_time = _to_utc_z(p["timestamp"])
            except ValueError:
                first_time = ""
            break

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<gpx version="1.1" creator="openhost-apple-health" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n',
        f"  <metadata><time>{first_time}</time></metadata>\n",
        f"  <trk><name>{escape(name or 'Route')}</name><trkseg>\n",
    ]
    for p in points:
        lat = p.get("latitude")
        lon = p.get("longitude")
        if lat is None or lon is None:
            continue
        seg = [f'    <trkpt lat={quoteattr(repr(lat))} lon={quoteattr(repr(lon))}>']
        if p.get("altitude") is not None:
            seg.append(f"<ele>{p['altitude']!r}</ele>")
        ts = p.get("timestamp")
        if ts:
            try:
                seg.append(f"<time>{_to_utc_z(ts)}</time>")
            except ValueError:
                pass
        ext = []
        if p.get("speed") is not None:
            ext.append(f"<speed>{p['speed']!r}</speed>")
        if p.get("course") is not None:
            ext.append(f"<course>{p['course']!r}</course>")
        if p.get("horizontalAccuracy") is not None:
            ext.append(f"<hAcc>{p['horizontalAccuracy']!r}</hAcc>")
        if p.get("verticalAccuracy") is not None:
            ext.append(f"<vAcc>{p['verticalAccuracy']!r}</vAcc>")
        if ext:
            seg.append("<extensions>" + "".join(ext) + "</extensions>")
        seg.append("</trkpt>\n")
        parts.append("".join(seg))
    parts.append("  </trkseg></trk>\n</gpx>\n")
    return gzip.compress("".join(parts).encode("utf-8"))


def _find_json_member(zf: zipfile.ZipFile) -> str | None:
    candidates = [n for n in zf.namelist() if n.lower().endswith(".json")]
    if not candidates:
        return None
    # The export's data file is the largest .json member.
    return max(candidates, key=lambda n: zf.getinfo(n).file_size)


@contextmanager
def _open_export(path: str):
    """Yield a binary stream of the export JSON, whether ``path`` is a plain
    JSON file or a zip wrapping one. Detected by content, not extension."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            member = _find_json_member(zf)
            if member is None:
                raise ValueError("No JSON data file found in zip")
            with zf.open(member) as f:
                yield f
    else:
        with open(path, "rb") as f:
            yield f


async def _update_job(conn, job_id: str, **fields) -> None:
    fields["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cols = ", ".join(f"{k} = ?" for k in fields)
    await conn.execute(
        f"UPDATE import_jobs SET {cols} WHERE id = ?",
        (*fields.values(), job_id),
    )
    await conn.commit()


def _count_items(path: str) -> tuple[int, int]:
    """Stream-count workouts and metric groups without building objects."""
    workouts = metrics = 0
    with _open_export(path) as f:
        for prefix, event, _ in ijson.parse(f):
            if event != "start_map":
                continue
            if prefix == "data.workouts.item":
                workouts += 1
            elif prefix == "data.metrics.item":
                metrics += 1
    return workouts, metrics


async def process_import(job_id: str, path: str) -> None:
    """Parse the uploaded export (zip or JSON) and ingest it, updating the import_jobs row."""
    from . import ingest  # lazy: ingest imports route_to_gpx from this module

    try:
        total_w, total_m = await asyncio.to_thread(_count_items, path)
        async with db.connect() as conn:
            await _update_job(conn, job_id, status="processing",
                              total_workouts=total_w, total_metrics=total_m)

        processed_w = 0
        async with db.connect() as conn:
            with _open_export(path) as f:
                for w in ijson.items(f, "data.workouts.item", use_float=True):
                    await ingest.save_workout(conn, w)
                    processed_w += 1
                    if processed_w % BATCH_COMMIT == 0:
                        await conn.commit()
                        await _update_job(conn, job_id, processed_workouts=processed_w)
            await conn.commit()
            await _update_job(conn, job_id, processed_workouts=processed_w)

        processed_m = 0
        if total_m:
            async with db.connect() as conn:
                with _open_export(path) as f:
                    for group in ijson.items(f, "data.metrics.item", use_float=True):
                        await ingest.save_metric_group(conn, group)
                        processed_m += 1
                        if processed_m % BATCH_COMMIT == 0:
                            await conn.commit()
                            await _update_job(conn, job_id, processed_metrics=processed_m)
                await conn.commit()
                await _update_job(conn, job_id, processed_metrics=processed_m)

        async with db.connect() as conn:
            await _update_job(conn, job_id, status="done")
        log.info("Import %s done: %d workouts, %d metric groups", job_id,
                 processed_w, processed_m)
    except Exception as e:
        log.exception("Import %s failed", job_id)
        async with db.connect() as conn:
            await _update_job(conn, job_id, status="error", error=str(e))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
