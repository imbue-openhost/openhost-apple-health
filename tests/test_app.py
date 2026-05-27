import httpx


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
        headers={"api-key": "sk-hae-a7x9mQ3vR2p"},
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
        headers={"api-key": "sk-hae-a7x9mQ3vR2p"},
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
    assert "metrics" in w
    assert "duration" in w


def test_service_sleep_sessions(stack):
    r = httpx.get(f"{stack.url}/api/v1/sleep-sessions")
    assert r.status_code == 200
    body = r.json()
    assert "data" in body
