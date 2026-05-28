import os
import aiosqlite
from contextlib import asynccontextmanager

DB_PATH = os.environ.get("OPENHOST_SQLITE_HEALTH", "health.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    date TEXT NOT NULL,
    qty REAL,
    units TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(metric_name, date, source)
);

CREATE TABLE IF NOT EXISTS heart_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    min_hr REAL NOT NULL,
    avg_hr REAL NOT NULL,
    max_hr REAL NOT NULL,
    units TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(date, source)
);

CREATE TABLE IF NOT EXISTS sleep_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    in_bed_start TEXT NOT NULL,
    in_bed_end TEXT NOT NULL,
    sleep_start TEXT NOT NULL,
    sleep_end TEXT NOT NULL,
    core REAL NOT NULL,
    rem REAL NOT NULL,
    deep REAL NOT NULL,
    awake REAL NOT NULL,
    in_bed REAL NOT NULL,
    units TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(date, source)
);

CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    duration REAL NOT NULL,
    active_energy_qty REAL,
    active_energy_units TEXT,
    distance_qty REAL,
    distance_units TEXT
);

CREATE TABLE IF NOT EXISTS workout_heart_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id TEXT NOT NULL REFERENCES workouts(workout_id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    min_hr REAL NOT NULL,
    avg_hr REAL NOT NULL,
    max_hr REAL NOT NULL,
    units TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_route (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id TEXT NOT NULL REFERENCES workouts(workout_id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude REAL,
    speed REAL,
    course REAL
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    payload TEXT NOT NULL,
    metrics_count INTEGER DEFAULT 0,
    workouts_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_metrics_name_date ON metrics(metric_name, date);
CREATE INDEX IF NOT EXISTS idx_heart_rate_date ON heart_rate(date);
CREATE INDEX IF NOT EXISTS idx_sleep_date ON sleep_analysis(date);
CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts(start_ts);
CREATE INDEX IF NOT EXISTS idx_workout_hr_wid ON workout_heart_rate(workout_id);
CREATE INDEX IF NOT EXISTS idx_workout_route_wid ON workout_route(workout_id);
"""


async def init_db():
    async with connect() as db:
        await db.executescript(SCHEMA)
        await db.commit()


@asynccontextmanager
async def connect():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()


async def get_config(key: str) -> str | None:
    async with connect() as db:
        cursor = await db.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_config(key: str, value: str):
    async with connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()
