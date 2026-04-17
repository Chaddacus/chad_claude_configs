"""SQLite experiment tracking database for the autoconfig system."""

import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = Path.home() / ".claude" / "state" / "autoconfig" / "experiments.db"

_connection: Optional[sqlite3.Connection] = None

SCHEMA_EXPERIMENTS = """\
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phase INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    mutation_summary TEXT NOT NULL,
    mutation_json TEXT NOT NULL,
    knobs_changed INTEGER DEFAULT 0,
    composite_score REAL,
    quality_score REAL,
    speed_score REAL,
    wall_time_seconds REAL,
    benchmark_results TEXT,
    kept INTEGER DEFAULT 0,
    baseline_score_before REAL,
    improvement_pct REAL,
    decision TEXT,
    trial_count INTEGER DEFAULT 1,
    confirmation_results TEXT,
    evaluation_version TEXT,
    run_mode TEXT,
    calibration_sample INTEGER DEFAULT 0,
    readiness_contribution INTEGER DEFAULT 0,
    error_message TEXT,
    config_hash TEXT,
    cumulative_improvement_pct REAL
)
"""

SCHEMA_DAILY_STATS = """\
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    experiment_count INTEGER DEFAULT 0,
    improvements_found INTEGER DEFAULT 0,
    rate_limit_hits INTEGER DEFAULT 0
)
"""


def init_db() -> sqlite3.Connection:
    """Create tables if they do not exist and return a connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA_EXPERIMENTS)
    conn.execute(SCHEMA_DAILY_STATS)
    _ensure_column(conn, "experiments", "decision", "TEXT")
    _ensure_column(conn, "experiments", "trial_count", "INTEGER DEFAULT 1")
    _ensure_column(conn, "experiments", "confirmation_results", "TEXT")
    _ensure_column(conn, "experiments", "evaluation_version", "TEXT")
    _ensure_column(conn, "experiments", "run_mode", "TEXT")
    _ensure_column(conn, "experiments", "calibration_sample", "INTEGER DEFAULT 0")
    _ensure_column(conn, "experiments", "readiness_contribution", "INTEGER DEFAULT 0")
    conn.commit()
    return conn


def get_db() -> sqlite3.Connection:
    """Return the singleton connection, creating it if necessary."""
    global _connection
    if _connection is None:
        _connection = init_db()
    return _connection


def _sanitize_improvement_pct(
    value: Optional[float],
    baseline_score_before: Optional[float],
    composite_score: Optional[float],
) -> Optional[float]:
    """Normalize non-finite improvement percentages from legacy rows."""
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(numeric):
        return numeric

    if (
        baseline_score_before is not None
        and composite_score is not None
        and baseline_score_before <= 0.0
    ):
        return float(composite_score) - float(baseline_score_before)
    return 0.0


def _sanitize_experiment_dict(row_dict: dict) -> dict:
    """Return a copy with finite improvement metrics."""
    sanitized = dict(row_dict)
    sanitized["improvement_pct"] = _sanitize_improvement_pct(
        sanitized.get("improvement_pct"),
        sanitized.get("baseline_score_before"),
        sanitized.get("composite_score"),
    )
    sanitized["cumulative_improvement_pct"] = _sanitize_improvement_pct(
        sanitized.get("cumulative_improvement_pct"),
        sanitized.get("baseline_score_before"),
        sanitized.get("composite_score"),
    )
    return sanitized


# ---------------------------------------------------------------------------
# Experiment lifecycle
# ---------------------------------------------------------------------------


def log_experiment_start(
    phase: int,
    mutation_summary: str,
    mutation_json: str,
    knobs_changed: int = 0,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
    calibration_sample: int = 0,
    readiness_contribution: int = 0,
) -> int:
    """Insert a new running experiment and return its id."""
    db = get_db()
    cur = db.execute(
        """\
        INSERT INTO experiments (phase, started_at, status, mutation_summary,
                                 mutation_json, knobs_changed, evaluation_version,
                                 run_mode, calibration_sample, readiness_contribution)
        VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            phase,
            datetime.now(timezone.utc).isoformat(),
            mutation_summary,
            mutation_json,
            knobs_changed,
            evaluation_version,
            run_mode,
            calibration_sample,
            readiness_contribution,
        ),
    )
    db.commit()
    return cur.lastrowid


def log_experiment_result(
    experiment_id: int,
    status: str,
    composite_score: Optional[float] = None,
    quality_score: Optional[float] = None,
    speed_score: Optional[float] = None,
    wall_time_seconds: Optional[float] = None,
    benchmark_results: Optional[str] = None,
    kept: int = 0,
    baseline_score_before: Optional[float] = None,
    improvement_pct: Optional[float] = None,
    decision: Optional[str] = None,
    trial_count: int = 1,
    confirmation_results: Optional[str] = None,
    error_message: Optional[str] = None,
    config_hash: Optional[str] = None,
    cumulative_improvement_pct: Optional[float] = None,
) -> None:
    """Update an experiment with its outcome."""
    db = get_db()
    improvement_pct = _sanitize_improvement_pct(
        improvement_pct,
        baseline_score_before,
        composite_score,
    )
    cumulative_improvement_pct = _sanitize_improvement_pct(
        cumulative_improvement_pct,
        baseline_score_before,
        composite_score,
    )
    db.execute(
        """\
        UPDATE experiments
           SET completed_at = ?,
               status = ?,
               composite_score = ?,
               quality_score = ?,
               speed_score = ?,
               wall_time_seconds = ?,
               benchmark_results = ?,
               kept = ?,
               baseline_score_before = ?,
               improvement_pct = ?,
               decision = ?,
               trial_count = ?,
               confirmation_results = ?,
               error_message = ?,
               config_hash = ?,
               cumulative_improvement_pct = ?
         WHERE id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            status,
            composite_score,
            quality_score,
            speed_score,
            wall_time_seconds,
            benchmark_results,
            kept,
            baseline_score_before,
            improvement_pct,
            decision,
            trial_count,
            confirmation_results,
            error_message,
            config_hash,
            cumulative_improvement_pct,
            experiment_id,
        ),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [_sanitize_experiment_dict(dict(row)) for row in rows]


def _append_filters(
    clauses: list[str],
    params: list[object],
    *,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> None:
    """Append common experiment filters to a SQL WHERE clause."""
    if evaluation_version is not None:
        clauses.append("evaluation_version = ?")
        params.append(evaluation_version)
    if run_mode is not None:
        clauses.append("COALESCE(run_mode, 'search') = ?")
        params.append(run_mode)


def _parse_benchmark_results(raw: Optional[str]) -> list[dict]:
    """Best-effort decode of serialized benchmark results."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    """Best-effort additive migration for older experiment DBs."""
    existing = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name in existing:
        return
    conn.execute(
        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"
    )


def get_experiment(experiment_id: int) -> Optional[dict]:
    """Return a single experiment as a dict, or None."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
    ).fetchone()
    return _sanitize_experiment_dict(dict(row)) if row else None


def get_recent_experiments(
    limit: int = 20,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> list[dict]:
    """Return the most recent experiments, newest first."""
    db = get_db()
    clauses: list[str] = []
    params: list[object] = []
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM experiments {where_sql}ORDER BY id DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_kept_experiments(
    limit: int = 50,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> list[dict]:
    """Return experiments that were kept, newest first."""
    db = get_db()
    clauses = ["kept = 1"]
    params: list[object] = []
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    params.append(limit)
    rows = db.execute(
        f"SELECT * FROM experiments WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_top_experiments(limit: int = 10) -> list[dict]:
    """Return experiments with the highest composite scores."""
    db = get_db()
    rows = db.execute(
        """\
        SELECT * FROM experiments
         WHERE composite_score IS NOT NULL
         ORDER BY composite_score DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_bottom_experiments(limit: int = 10) -> list[dict]:
    """Return experiments with the lowest composite scores."""
    db = get_db()
    rows = db.execute(
        """\
        SELECT * FROM experiments
         WHERE composite_score IS NOT NULL
         ORDER BY composite_score ASC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_experiments_by_phase(
    phase: int,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> list[dict]:
    """Return all experiments for a given phase, newest first."""
    db = get_db()
    clauses = ["phase = ?"]
    params: list[object] = [phase]
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    rows = db.execute(
        f"SELECT * FROM experiments WHERE {' AND '.join(clauses)} ORDER BY id DESC",
        tuple(params),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_consecutive_discards(
    phase: Optional[int] = None,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> int:
    """Count most-recent completed discards, optionally scoped to one phase."""
    db = get_db()
    clauses: list[str] = []
    params: list[object] = []
    if phase is not None:
        clauses.append("phase = ?")
        params.append(phase)
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    where_sql = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    rows = db.execute(
        f"SELECT kept, status FROM experiments {where_sql}ORDER BY id DESC",
        tuple(params),
    ).fetchall()
    count = 0
    for row in rows:
        if row["status"] == "running":
            continue
        if row["kept"] == 0:
            count += 1
        else:
            break
    return count


def get_tried_mutations(
    phase: int,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> set[str]:
    """Return the set of mutation_summary strings already attempted in a phase."""
    db = get_db()
    clauses = ["phase = ?"]
    params: list[object] = [phase]
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    rows = db.execute(
        f"SELECT DISTINCT mutation_summary FROM experiments WHERE {' AND '.join(clauses)}",
        tuple(params),
    ).fetchall()
    return {row["mutation_summary"] for row in rows}


def get_recent_benchmark_results(
    benchmark_id: str,
    limit: int = 10,
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> list[dict]:
    """Return recent benchmark result payloads for one benchmark id.

    Results are newest first and include parent experiment metadata.
    Only completed experiments with serialized benchmark results are scanned.
    """
    db = get_db()
    clauses = ["status = 'completed'", "benchmark_results IS NOT NULL"]
    params: list[object] = []
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    rows = db.execute(
        f"""\
            SELECT id, phase, started_at, completed_at, evaluation_version, run_mode,
                   calibration_sample, readiness_contribution, benchmark_results
              FROM experiments
             WHERE {' AND '.join(clauses)}
             ORDER BY id DESC
            """,
        tuple(params),
    ).fetchall()

    results: list[dict] = []
    for row in rows:
        row_dict = dict(row)
        for result in _parse_benchmark_results(row_dict.get("benchmark_results")):
            if result.get("benchmark_id") != benchmark_id:
                continue
            enriched = dict(result)
            enriched["experiment_id"] = row_dict["id"]
            enriched["phase"] = row_dict["phase"]
            enriched["started_at"] = row_dict["started_at"]
            enriched["completed_at"] = row_dict["completed_at"]
            enriched["evaluation_version"] = row_dict.get("evaluation_version")
            enriched["run_mode"] = row_dict.get("run_mode")
            enriched["calibration_sample"] = row_dict.get("calibration_sample")
            enriched["readiness_contribution"] = row_dict.get("readiness_contribution")
            results.append(enriched)
            if len(results) >= limit:
                return results
    return results


# ---------------------------------------------------------------------------
# Daily stats
# ---------------------------------------------------------------------------


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def increment_daily_stats(field: str) -> None:
    """Increment one of experiment_count, improvements_found, or rate_limit_hits for today."""
    allowed = {"experiment_count", "improvements_found", "rate_limit_hits"}
    if field not in allowed:
        raise ValueError(f"field must be one of {allowed}, got {field!r}")

    db = get_db()
    today = _today()
    db.execute(
        "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)", (today,)
    )
    # field is validated above so this is safe from injection.
    db.execute(
        f"UPDATE daily_stats SET {field} = {field} + 1 WHERE date = ?",
        (today,),
    )
    db.commit()


def get_daily_stats(date: Optional[str] = None) -> dict:
    """Return daily stats for a date (YYYY-MM-DD), defaulting to today."""
    db = get_db()
    target = date or _today()
    row = db.execute(
        "SELECT * FROM daily_stats WHERE date = ?", (target,)
    ).fetchone()
    if row:
        return dict(row)
    return {
        "date": target,
        "experiment_count": 0,
        "improvements_found": 0,
        "rate_limit_hits": 0,
    }


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


def get_experiment_count(
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> int:
    """Return the total number of experiments."""
    db = get_db()
    clauses: list[str] = []
    params: list[object] = []
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = db.execute(
        f"SELECT COUNT(*) AS cnt FROM experiments {where_sql}",
        tuple(params),
    ).fetchone()
    return row["cnt"]


def get_total_kept(
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> int:
    """Return the total number of kept experiments."""
    db = get_db()
    clauses = ["kept = 1"]
    params: list[object] = []
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    row = db.execute(
        f"SELECT COUNT(*) AS cnt FROM experiments WHERE {' AND '.join(clauses)}",
        tuple(params),
    ).fetchone()
    return row["cnt"]


def get_cumulative_improvement(
    evaluation_version: Optional[str] = None,
    run_mode: Optional[str] = None,
) -> float:
    """Return the cumulative_improvement_pct from the most recent kept experiment, or 0.0."""
    db = get_db()
    clauses = ["kept = 1", "cumulative_improvement_pct IS NOT NULL"]
    params: list[object] = []
    _append_filters(
        clauses,
        params,
        evaluation_version=evaluation_version,
        run_mode=run_mode,
    )
    row = db.execute(
        f"""\
            SELECT cumulative_improvement_pct,
                   improvement_pct,
                   baseline_score_before,
                   composite_score
              FROM experiments
             WHERE {' AND '.join(clauses)}
             ORDER BY id DESC
             LIMIT 1
            """,
        tuple(params),
    ).fetchone()
    if row:
        cumulative = _sanitize_improvement_pct(
            row["cumulative_improvement_pct"],
            row["baseline_score_before"],
            row["composite_score"],
        )
        if cumulative is not None:
            return cumulative
    return 0.0
