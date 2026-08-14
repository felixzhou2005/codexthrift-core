from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class HistoryStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS routing_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT,
                    repo_hash TEXT NOT NULL,
                    task_fingerprint TEXT NOT NULL,
                    baseline_tier TEXT NOT NULL,
                    selected_model TEXT,
                    reasoning_effort TEXT,
                    selection_reason TEXT,
                    exploration_flag INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    feature_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_outcomes (
                    task_id TEXT PRIMARY KEY REFERENCES routing_tasks(task_id),
                    first_pass INTEGER NOT NULL,
                    final_success INTEGER NOT NULL,
                    escalation_used INTEGER NOT NULL,
                    infrastructure_failure INTEGER NOT NULL,
                    reward REAL,
                    reward_confidence TEXT NOT NULL,
                    reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_attempts (
                    task_id TEXT NOT NULL REFERENCES routing_tasks(task_id),
                    attempt_index INTEGER NOT NULL,
                    model TEXT,
                    success INTEGER NOT NULL,
                    codex_returncode INTEGER,
                    verification_json TEXT NOT NULL,
                    PRIMARY KEY(task_id, attempt_index)
                );
                CREATE TABLE IF NOT EXISTS bandit_state (
                    action TEXT PRIMARY KEY,
                    alpha_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def record_task(self, task: dict[str, Any], outcome: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO routing_tasks (
                    task_id, session_id, repo_hash, task_fingerprint, baseline_tier,
                    selected_model, reasoning_effort, selection_reason, exploration_flag,
                    started_at, finished_at, feature_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    finished_at=excluded.finished_at, selected_model=excluded.selected_model,
                    reasoning_effort=excluded.reasoning_effort, feature_json=excluded.feature_json""",
                (
                    task["task_id"], task.get("session_id"), task["repo_hash"], task["task_fingerprint"],
                    task.get("baseline_tier", "MEDIUM"), task.get("selected_model"), task.get("reasoning_effort"),
                    task.get("selection_reason", "task-run"), int(bool(task.get("exploration_flag"))),
                    task.get("started_at"), task.get("finished_at"), json.dumps(task["features"], ensure_ascii=False),
                ),
            )
            connection.execute(
                """INSERT INTO task_outcomes (
                    task_id, first_pass, final_success, escalation_used, infrastructure_failure,
                    reward, reward_confidence, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    first_pass=excluded.first_pass, final_success=excluded.final_success,
                    escalation_used=excluded.escalation_used, infrastructure_failure=excluded.infrastructure_failure,
                    reward=excluded.reward, reward_confidence=excluded.reward_confidence, reason=excluded.reason""",
                (
                    task["task_id"], int(bool(outcome.get("first_pass"))), int(bool(outcome.get("final_success"))),
                    int(outcome.get("escalation_used", 0)), int(bool(outcome.get("infrastructure_failure"))),
                    outcome.get("reward"), outcome["confidence"], outcome["reason"],
                ),
            )
            for index, attempt in enumerate(attempts):
                connection.execute(
                    """INSERT INTO task_attempts (
                        task_id, attempt_index, model, success, codex_returncode, verification_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, attempt_index) DO UPDATE SET
                        model=excluded.model, success=excluded.success, codex_returncode=excluded.codex_returncode,
                        verification_json=excluded.verification_json""",
                    (
                        task["task_id"], index, attempt.get("model"), int(bool(attempt.get("success"))),
                        attempt.get("codex", {}).get("returncode") if isinstance(attempt.get("codex"), dict) else None,
                        json.dumps(attempt.get("verification") or [], ensure_ascii=False),
                    ),
                )

    def verified_tasks(self, *, limit: int = 500) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT t.*, o.first_pass, o.final_success, o.escalation_used,
                          o.infrastructure_failure, o.reward, o.reward_confidence, o.reason
                   FROM routing_tasks t JOIN task_outcomes o USING(task_id)
                   WHERE o.reward IS NOT NULL
                   ORDER BY t.finished_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [{**dict(row), "features": json.loads(row["feature_json"])} for row in rows]

    def summary(self) -> dict[str, Any]:
        self.initialize()
        with self._connect() as connection:
            counts = connection.execute(
                """SELECT COUNT(*) AS tasks, SUM(final_success) AS successes,
                          SUM(first_pass) AS first_passes, SUM(escalation_used) AS escalations,
                          SUM(infrastructure_failure) AS infrastructure_failures
                   FROM task_outcomes"""
            ).fetchone()
            strong = connection.execute(
                "SELECT COUNT(*) AS count FROM task_outcomes WHERE reward_confidence='strong'"
            ).fetchone()
        return {
            "database_path": str(self.database_path),
            "tasks": int(counts["tasks"] or 0),
            "successes": int(counts["successes"] or 0),
            "first_passes": int(counts["first_passes"] or 0),
            "escalations": int(counts["escalations"] or 0),
            "infrastructure_failures": int(counts["infrastructure_failures"] or 0),
            "strong_feedback_tasks": int(strong["count"] or 0),
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection
