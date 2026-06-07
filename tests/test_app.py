import io
import json
import time
import zipfile

import httpx


def _api_key(stack) -> str:
    """The app generates the write token on first boot; read it back via settings."""
    return httpx.get(f"{stack.url}/api/v1/settings").json()["api_key"]


def _export_payload(workout_id: str = "manual-1") -> dict:
    """A minimal Health Auto Export payload: one workout with a 2-point route."""
    payload = {
        "data": {
            "workouts": [
                {
                    "id": workout_id,
                    "name": "Outdoor Run",
                    "start": "2026-03-06 10:15:06 -0800",
                    "end": "2026-03-06 10:26:56 -0800",
                    "duration": 710.0,
                    "activeEnergyBurned": {"qty": 100.0, "units": "kcal"},
                    "distance": {"qty": 2.0, "units": "mi"},
                    "avgHeartRate": {"qty": 150.0, "units": "count/min"},
                    "maxHeartRate": {"qty": 170.0, "units": "count/min"},
                    "heartRate": {
                        "min": {"qty": 100, "units": "count/min"},
                        "avg": {"qty": 150, "units": "count/min"},
                        "max": {"qty": 170, "units": "count/min"},
                    },
                    "elevationUp": {"qty": 50.0, "units": "ft"},
                    "temperature": {"qty": 61.7, "units": "degF"},
                    "isIndoor": False,
                    "activeEnergy": [
                        {"date": "2026-03-06 10:15:06 -0800", "qty": 5.0, "units": "kcal", "source": "Watch"},
                    ],
                    "stepCount": [
                        {"date": "2026-03-06 10:16:06 -0800", "qty": 40.0, "units": "count", "source": "iPhone"},
                    ],
                    "heartRateData": [
                        {"date": "2026-03-06 10:16:06 -0800", "Min": 140, "Avg": 150, "Max": 160, "units": "count/min", "source": "Watch"},
                    ],
                    "heartRateRecovery": [
                        {"date": "2026-03-06 10:27:00 -0800", "Min": 120, "Avg": 120, "Max": 120, "units": "count/min", "source": "Watch"},
                    ],
                    "route": [
                        {"latitude": 37.0, "longitude": -122.0, "altitude": 10.0, "speed": 2.0,
                         "course": 90.0, "horizontalAccuracy": 3.0, "verticalAccuracy": 2.0,
                         "timestamp": "2026-03-06 10:15:07 -0800"},
                        {"latitude": 37.001, "longitude": -122.001, "altitude": 11.0, "speed": 2.1,
                         "course": 91.0, "horizontalAccuracy": 3.0, "verticalAccuracy": 2.0,
                         "timestamp": "2026-03-06 10:15:08 -0800"},
                    ],
                }
            ]
        }
    }
    return payload


def _export_zip(workout_id: str = "manual-1") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("HealthAutoExport-2026.json", json.dumps(_export_payload(workout_id)))
    return buf.getvalue()


def _export_json(workout_id: str = "manual-1") -> bytes:
    return json.dumps(_export_payload(workout_id)).encode("utf-8")


def test_health(stack):
    r = httpx.get(f"{stack.app_url}/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_serves_html(stack):
    r = httpx.get(f"{stack.url}/")
    assert r.status_code == 200
    assert "Apple Health" in r.text


def test_ingest_requires_auth(stack):
    r = httpx.post(f"{stack.url}/api/data", json={"data": {}})
    assert r.status_code == 401


def test_settings(stack):
    r = httpx.get(f"{stack.url}/api/v1/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"].startswith("sk-hae-")
    assert body["upload_url"].endswith("/api/data")


def test_ingest_metrics(stack):
    payload = {
        "data": {
            "metrics": [
                {
                    "name": "heart_rate",
                    "units": "bpm",
                    "data": [
                        {"date": "2025-01-01T10:00:00-05:00", "Min": 60, "Avg": 72, "Max": 85, "source": "Apple Watch"},
                        {"date": "2025-01-01T10:05:00-05:00", "Min": 58, "Avg": 70, "Max": 80, "source": "Apple Watch"},
                    ],
                },
                {
                    "name": "active_energy",
                    "units": "kcal",
                    "data": [
                        {"date": "2025-01-01T10:00:00-05:00", "qty": 42.5, "source": "Apple Watch"},
                    ],
                },
            ]
        }
    }
    r = httpx.post(
        f"{stack.url}/api/data",
        json=payload,
        headers={"api-key": _api_key(stack)},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["success"] is True


def test_query_heart_rate(stack):
    r = httpx.get(f"{stack.url}/api/v1/heart-rate")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 2


def test_service_time_series(stack):
    r = httpx.get(f"{stack.url}/api/v1/time-series?metric=heart_rate")
    assert r.status_code == 200
    body = r.json()
    assert body["metric_id"] == "heart_rate"
    assert body["unit"] == "bpm"
    assert len(body["samples"]) >= 2

    r2 = httpx.get(f"{stack.url}/api/v1/time-series?metric=active_energy")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["metric_id"] == "active_energy"
    assert len(body2["samples"]) >= 1


def test_service_list_metrics(stack):
    r = httpx.get(f"{stack.url}/api/v1/metrics")
    assert r.status_code == 200
    metrics = r.json()["metrics"]
    assert len(metrics) >= 2
    ids = [m["metric_id"] for m in metrics]
    assert "heart_rate" in ids
    assert "active_energy" in ids
    for m in metrics:
        assert "kind" in m
        assert "display_name" in m


def test_stats(stack):
    r = httpx.get(f"{stack.url}/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["heart_rate_samples"] >= 2
    assert body["payloads_received"] >= 1


def test_ingest_workouts(stack):
    payload = {
        "data": {
            "workouts": [
                {
                    "id": "test-workout-001",
                    "name": "Running",
                    "start": "2025-01-01T07:00:00-05:00",
                    "end": "2025-01-01T07:30:00-05:00",
                    "duration": 1800,
                    "activeEnergyBurned": {"qty": 250, "units": "kcal"},
                    "distance": {"qty": 5.2, "units": "km"},
                    "heartRateData": [
                        {"date": "2025-01-01T07:05:00-05:00", "Min": 120, "Avg": 145, "Max": 160, "units": "bpm", "source": "Apple Watch"},
                    ],
                }
            ]
        }
    }
    r = httpx.post(
        f"{stack.url}/api/data",
        json=payload,
        headers={"api-key": _api_key(stack)},
    )
    assert r.status_code == 200
    assert r.json()["workouts"]["success"] is True


def test_service_workouts(stack):
    r = httpx.get(f"{stack.url}/api/v1/workouts")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    w = body["data"][0]
    assert w["workout_type"] == "running"
    assert "duration" in w
    # The free-form metrics dict was removed; only spec fields are returned.
    assert "metrics" not in w
    # distance was ingested in km and is exposed in meters per the spec.
    assert w["distance"]["unit"] == "m"
    assert abs(w["distance"]["value"] - 5.2 * 1000) < 1
    # The list is a summary: the per-sample HR trace and route are omitted.
    assert "heart_rate" not in w
    assert "route_gpx" not in w


def test_service_workout_detail(stack):
    r = httpx.get(f"{stack.url}/api/v1/workouts/test-workout-001")
    assert r.status_code == 200
    w = r.json()
    assert w["id"] == "test-workout-001"
    assert "duration" in w
    # Detail carries the full heart-rate trace the list omits.
    assert w["heart_rate"]["samples"][0]["value"] == 145


def test_service_workout_detail_404(stack):
    r = httpx.get(f"{stack.url}/api/v1/workouts/does-not-exist")
    assert r.status_code == 404


def test_service_sleep_sessions(stack):
    r = httpx.get(f"{stack.url}/api/v1/sleep-sessions")
    assert r.status_code == 200
    body = r.json()
    assert "data" in body


def test_manual_import(stack):
    r = httpx.post(
        f"{stack.url}/api/import",
        content=_export_zip(),
        headers={"x-filename": "export.zip"},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    status = {}
    for _ in range(100):
        status = httpx.get(f"{stack.url}/api/import/{job_id}").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert status["status"] == "done", status
    assert status["processed_workouts"] == 1

    # Route was regenerated as GPX and is downloadable.
    g = httpx.get(f"{stack.url}/workouts/manual-1/route.gpx")
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("application/gpx+xml")
    assert g.text.count("<trkpt") == 2

    # Enriched workout is exposed via the service, in spec form/units.
    data = httpx.get(f"{stack.url}/api/v1/workouts").json()["data"]
    w = next(x for x in data if x["id"] == "manual-1")
    assert w["workout_type"] == "running"
    assert "metrics" not in w
    assert abs(w["distance"]["value"] - 2 * 1609.344) < 1  # mi -> m
    assert w["distance"]["unit"] == "m"
    assert abs(w["temperature"]["value"] - 16.5) < 0.5  # degF -> degC
    assert w["lowest_heart_rate"]["value"] == 100
    assert w["is_indoor"] is False
    assert "average_pace" in w


def test_manual_import_reimport_is_idempotent(stack):
    """Re-importing the same workout replaces rather than duplicates child rows."""
    before = httpx.get(f"{stack.url}/workouts/manual-1/route.gpx").text.count("<trkpt")
    r = httpx.post(
        f"{stack.url}/api/import",
        content=_export_zip(),
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    for _ in range(100):
        status = httpx.get(f"{stack.url}/api/import/{job_id}").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert status["status"] == "done", status
    after = httpx.get(f"{stack.url}/workouts/manual-1/route.gpx").text.count("<trkpt")
    assert after == before == 2


def test_manual_import_plain_json(stack):
    """A plain .json export (no zip wrapper) is accepted too, route and all."""
    r = httpx.post(
        f"{stack.url}/api/import",
        content=_export_json("manual-json-1"),
        headers={"x-filename": "export.json"},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    status = {}
    for _ in range(100):
        status = httpx.get(f"{stack.url}/api/import/{job_id}").json()
        if status["status"] in ("done", "error"):
            break
        time.sleep(0.2)
    assert status["status"] == "done", status
    assert status["processed_workouts"] == 1

    g = httpx.get(f"{stack.url}/workouts/manual-json-1/route.gpx")
    assert g.status_code == 200
    assert g.text.count("<trkpt") == 2


def test_workouts_ordered_and_filtered_by_instant_not_string(stack):
    """Offset-bearing timestamps must order/filter by true UTC instant.

    tz-late is chronologically later than tz-early (06:30Z vs 23:00Z the prior
    day) but sorts *earlier* as a raw string, so a lexical compare would both
    misorder the list and wrongly drop tz-late from a UTC `start` window.
    """
    payload = {
        "data": {
            "workouts": [
                {"id": "tz-late", "name": "Running",
                 "start": "2026-04-02T23:30:00-07:00", "end": "2026-04-02T23:45:00-07:00"},
                {"id": "tz-early", "name": "Running",
                 "start": "2026-04-03T01:00:00+02:00", "end": "2026-04-03T01:15:00+02:00"},
            ]
        }
    }
    r = httpx.post(f"{stack.url}/api/data", json=payload, headers={"api-key": _api_key(stack)})
    assert r.status_code == 200

    # Full list is ascending by instant, so tz-late (later instant) comes after tz-early.
    ids = [w["id"] for w in httpx.get(f"{stack.url}/api/v1/workouts?limit=5000").json()["data"]]
    assert ids.index("tz-late") > ids.index("tz-early")

    # A UTC `start` window between the two instants includes only tz-late.
    window = httpx.get(f"{stack.url}/api/v1/workouts?limit=5000&start=2026-04-03T04:00:00Z").json()["data"]
    window_ids = {w["id"] for w in window}
    assert "tz-late" in window_ids
    assert "tz-early" not in window_ids
