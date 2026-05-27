import logging
import os

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


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apple Health</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 1.5rem; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }
  h1 { font-size: 1.5rem; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.75rem;
           margin-bottom: 1.5rem; }
  .stat { background: #1e293b; border-radius: 10px; padding: 1rem; text-align: center; }
  .stat .value { font-size: 1.75rem; font-weight: 700; }
  .stat .label { font-size: 0.75rem; color: #94a3b8; margin-top: 0.25rem; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 1rem; }
  .card { background: #1e293b; border-radius: 12px; padding: 1.25rem; }
  .card h2 { font-size: 1rem; color: #94a3b8; margin-bottom: 0.75rem; }
  canvas { width: 100% !important; }
  .empty { text-align: center; padding: 3rem; color: #64748b; }
  .range-btns { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
  .range-btn { padding: 0.3rem 0.75rem; border-radius: 6px; border: 1px solid #334155;
               background: transparent; color: #94a3b8; font-size: 0.8rem; cursor: pointer; }
  .range-btn.active { background: #6366f1; color: white; border-color: #6366f1; }
</style>
</head>
<body>
<div class="header">
  <h1>Apple Health</h1>
</div>

<div class="stats" id="stats">
  <div class="stat"><div class="value" id="stat-hr">--</div><div class="label">HR Samples</div></div>
  <div class="stat"><div class="value" id="stat-metrics">--</div><div class="label">Metric Samples</div></div>
  <div class="stat"><div class="value" id="stat-sleep">--</div><div class="label">Sleep Sessions</div></div>
  <div class="stat"><div class="value" id="stat-workouts">--</div><div class="label">Workouts</div></div>
  <div class="stat"><div class="value" id="stat-last">--</div><div class="label">Last Received</div></div>
</div>

<div class="range-btns">
  <button class="range-btn" data-days="1">24h</button>
  <button class="range-btn active" data-days="7">7d</button>
  <button class="range-btn" data-days="30">30d</button>
  <button class="range-btn" data-days="90">90d</button>
</div>

<div class="grid">
  <div class="card"><h2>Heart Rate</h2><canvas id="hrChart"></canvas></div>
  <div class="card"><h2>Heart Rate (Recent Detail)</h2><canvas id="hrRecent"></canvas></div>
</div>

<script>
const COLORS = {
  indigo: '#6366f1', cyan: '#06b6d4', emerald: '#10b981', amber: '#f59e0b',
  rose: '#f43f5e', purple: '#a855f7', slate: '#64748b',
};
const chartOpts = {
  responsive: true,
  plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 } } } },
  scales: {
    x: { ticks: { color: '#64748b', font: { size: 10 }, maxTicksLimit: 20 }, grid: { color: '#1e293b' } },
    y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e293b' } },
  },
};

let hrChart, hrRecent;
let currentDays = 7;

function isoAgo(days) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

async function fetchJSON(url) { return (await fetch(url)).json(); }

function fmtDate(iso) {
  if (!iso) return '--';
  const d = new Date(iso);
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' +
         d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

async function loadStats() {
  const s = await fetchJSON('/api/v1/stats');
  document.getElementById('stat-hr').textContent = s.heart_rate_samples.toLocaleString();
  document.getElementById('stat-metrics').textContent = s.metric_samples.toLocaleString();
  document.getElementById('stat-sleep').textContent = s.sleep_sessions;
  document.getElementById('stat-workouts').textContent = s.workouts;
  document.getElementById('stat-last').textContent = s.last_received ? fmtDate(s.last_received) : 'never';
}

async function loadHR(days) {
  const start = isoAgo(days);
  const data = await fetchJSON('/api/v1/heart-rate?start=' + encodeURIComponent(start));

  if (hrChart) hrChart.destroy();
  if (data.count === 0) return;

  const labels = data.data.map(d => {
    const dt = new Date(d.date);
    return days <= 1
      ? dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
      : dt.toLocaleDateString([], {month:'short', day:'numeric'}) + ' ' + dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  });

  hrChart = new Chart(document.getElementById('hrChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Avg', data: data.data.map(d => d.avg), borderColor: COLORS.rose, tension: 0.3, pointRadius: 0 },
        { label: 'Min', data: data.data.map(d => d.min), borderColor: COLORS.cyan, tension: 0.3, pointRadius: 0, borderDash: [4,2] },
        { label: 'Max', data: data.data.map(d => d.max), borderColor: COLORS.amber, tension: 0.3, pointRadius: 0, borderDash: [4,2] },
      ],
    },
    options: chartOpts,
  });
}

async function loadHRRecent() {
  const start = isoAgo(1);
  const data = await fetchJSON('/api/v1/heart-rate?start=' + encodeURIComponent(start) + '&limit=500');

  if (hrRecent) hrRecent.destroy();
  if (data.count === 0) return;

  const labels = data.data.map(d => new Date(d.date).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}));

  hrRecent = new Chart(document.getElementById('hrRecent'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'HR (bpm)', data: data.data.map(d => d.avg), borderColor: COLORS.rose, tension: 0.3, pointRadius: 0,
          fill: true, backgroundColor: 'rgba(244,63,94,0.1)' },
      ],
    },
    options: chartOpts,
  });
}

async function load(days) {
  currentDays = days;
  await Promise.all([loadStats(), loadHR(days), loadHRRecent()]);
}

document.querySelectorAll('.range-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    load(parseInt(btn.dataset.days));
  });
});

load(7);
</script>
</body>
</html>"""


app = Litestar(
    route_handlers=[
        health_check, index, ingest_data,
        get_heart_rate, get_stats,
        service_list_metrics, service_get_time_series,
        service_get_sleep_sessions, service_get_workouts,
    ],
    on_startup=[on_startup],
)
