import os
import secrets
import aiosqlite
from contextlib import asynccontextmanager

DB_PATH = os.environ.get("OPENHOST_SQLITE_HEALTH", "health.db")

WRITE_TOKEN_KEY = "write_token"

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
    distance_units TEXT,
    is_indoor INTEGER,
    location TEXT
);

CREATE TABLE IF NOT EXISTS workout_heart_rate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id TEXT NOT NULL REFERENCES workouts(workout_id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    min_hr REAL NOT NULL,
    avg_hr REAL NOT NULL,
    max_hr REAL NOT NULL,
    units TEXT NOT NULL,
    source TEXT NOT NULL,
    series TEXT NOT NULL DEFAULT 'heartRate'
);

-- Summary scalar fields per workout (every {qty, units} field, generically).
CREATE TABLE IF NOT EXISTS workout_scalars (
    workout_id TEXT NOT NULL REFERENCES workouts(workout_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    qty REAL,
    units TEXT,
    UNIQUE(workout_id, name)
);

-- Per-minute quantity time series per workout (activeEnergy, stepCount, etc).
CREATE TABLE IF NOT EXISTS workout_time_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_id TEXT NOT NULL REFERENCES workouts(workout_id) ON DELETE CASCADE,
    series_name TEXT NOT NULL,
    date TEXT NOT NULL,
    qty REAL,
    units TEXT,
    source TEXT
);

-- GPS route stored as a gzipped GPX document, one per workout.
CREATE TABLE IF NOT EXISTS workout_routes (
    workout_id TEXT PRIMARY KEY REFERENCES workouts(workout_id) ON DELETE CASCADE,
    point_count INTEGER NOT NULL,
    gpx_gzip BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS import_jobs (
    id TEXT PRIMARY KEY,
    filename TEXT,
    status TEXT NOT NULL,
    total_workouts INTEGER DEFAULT 0,
    processed_workouts INTEGER DEFAULT 0,
    total_metrics INTEGER DEFAULT 0,
    processed_metrics INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
CREATE INDEX IF NOT EXISTS idx_workout_scalars_wid ON workout_scalars(workout_id);
CREATE INDEX IF NOT EXISTS idx_workout_ts_wid ON workout_time_series(workout_id);
"""

# Columns added to pre-existing tables. CREATE TABLE IF NOT EXISTS won't alter
# an already-created table, so add them explicitly if missing.
MIGRATIONS = [
    ("workouts", "is_indoor", "INTEGER"),
    ("workouts", "location", "TEXT"),
    ("workout_heart_rate", "series", "TEXT NOT NULL DEFAULT 'heartRate'"),
]


async def init_db():
    async with connect() as db:
        await db.executescript(SCHEMA)
        await _migrate(db)
        await db.commit()


async def _migrate(db):
    for table, column, decl in MIGRATIONS:
        cols = await (await db.execute(f"PRAGMA table_info({table})")).fetchall()
        if column not in {c[1] for c in cols}:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


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


async def ensure_write_token() -> str:
    """Return the upload write token, generating and persisting one on first call."""
    token = await get_config(WRITE_TOKEN_KEY)
    if token is None:
        token = "sk-hae-" + secrets.token_urlsafe(24)
        await set_config(WRITE_TOKEN_KEY, token)
    return token
