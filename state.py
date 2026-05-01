"""
state.py — zarządzanie stanem zadań przez SQLite
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from config import config


# ─────────────────────────────────────────────
# Statusy zadania
# ─────────────────────────────────────────────

class TaskStatus(str, Enum):
    NEW = "NEW"
    ARCHITECTING = "ARCHITECTING"
    ANALYZING = "ANALYZING"
    IMPLEMENTING = "IMPLEMENTING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    HUMAN_FEEDBACK = "HUMAN_FEEDBACK"
    REVIEWING = "REVIEWING"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    STUCK = "STUCK"
    FAILED = "FAILED"


# ─────────────────────────────────────────────
# Model kryterium akceptacji
# ─────────────────────────────────────────────

@dataclass
class Criterion:
    id: str
    description: str
    status: str = "PENDING"   # PENDING | DONE | FAILED
    evidence: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Criterion":
        return Criterion(**d)


# ─────────────────────────────────────────────
# Wpis historii jednej iteracji
# ─────────────────────────────────────────────

@dataclass
class IterationRecord:
    iteration: int
    diff_stat: str = ""            # "+X/-Y lines changed"
    diff_lines_changed: int = 0
    review_passed: bool = False
    open_criteria: list = field(default_factory=list)
    notes: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "IterationRecord":
        return IterationRecord(**d)


# ─────────────────────────────────────────────
# Główny model zadania
# ─────────────────────────────────────────────

@dataclass
class Task:
    task_id: str
    description: str
    title: str = ""
    status: TaskStatus = TaskStatus.NEW
    iteration: int = 0
    max_iterations: int = field(default_factory=lambda: config.max_iterations)
    stuck_counter: int = 0
    criteria: list = field(default_factory=list)   # List[Criterion]
    history: list = field(default_factory=list)    # List[IterationRecord]
    architect_plan: str = ""
    last_diff: str = ""
    task_start_sha: str = ""          # SHA commita sprzed startu — do pełnego diffa
    human_feedback: str = ""          # feedback człowieka gdy powiedział "fail"
    fix_plan: str = ""                # plan naprawy Claude'a (z human feedback lub code review)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # ── Pomocnicze ──

    def all_criteria_done(self) -> bool:
        return bool(self.criteria) and all(
            c["status"] == "DONE" for c in self.criteria
        )

    def open_criteria_list(self) -> list[dict]:
        return [c for c in self.criteria if c["status"] != "DONE"]

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, ensure_ascii=False, indent=2)

    @staticmethod
    def from_dict(d: dict) -> "Task":
        d["status"] = TaskStatus(d["status"])
        d.setdefault("human_feedback", "")
        d.setdefault("fix_plan", "")
        if "title" not in d:
            # Fallback dla starych zadań
            first_line = d.get("description", "").split("\n")[0]
            d["title"] = first_line[:100]
        return Task(**d)


# ─────────────────────────────────────────────
# Repozytorium (SQLite)
# ─────────────────────────────────────────────

class TaskRepository:
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        # Upewnij się, że katalog dla bazy danych (.orchestrator) istnieje
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id     TEXT PRIMARY KEY,
                    status      TEXT NOT NULL,
                    iteration   INTEGER NOT NULL DEFAULT 0,
                    data        TEXT NOT NULL,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                )
            """)

    # ── CRUD ──

    def save(self, task: Task) -> None:
        task.updated_at = time.time()
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO tasks (task_id, status, iteration, data, created_at, updated_at)
                VALUES (:id, :status, :iter, :data, :created, :updated)
                ON CONFLICT(task_id) DO UPDATE SET
                    status     = excluded.status,
                    iteration  = excluded.iteration,
                    data       = excluded.data,
                    updated_at = excluded.updated_at
            """, {
                "id": task.task_id,
                "status": task.status.value,
                "iter": task.iteration,
                "data": task.to_json(),
                "created": task.created_at,
                "updated": task.updated_at,
            })

    def load(self, task_id: str) -> Optional[Task]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return Task.from_dict(json.loads(row["data"]))

    def list_all(self) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM tasks ORDER BY created_at DESC"
            ).fetchall()
        return [Task.from_dict(json.loads(r["data"])) for r in rows]

    def list_by_status(self, status: TaskStatus) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM tasks WHERE status = ? ORDER BY created_at",
                (status.value,)
            ).fetchall()
        return [Task.from_dict(json.loads(r["data"])) for r in rows]
