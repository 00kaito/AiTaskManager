"""
runner.py — główna pętla orkiestratora

Użycie:
    python runner.py new "Zrefaktoruj moduł parsera do nowej architektury"
    python runner.py run TASK-001
    python runner.py status
    python runner.py status TASK-001
"""

import json
import logging
import sys
import time
import uuid
from pathlib import Path

from agents import ClaudeAgent, GeminiAgent, GitHelper
from config import config, override_from_env
from prompts import (
    architect_prompt,
    build_codebase_summary,
    implement_prompt,
    review_prompt,
)
from state import Criterion, IterationRecord, Task, TaskRepository, TaskStatus


# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def setup_logging(task_id: str = "") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if config.log_to_file and task_id:
        run_dir = config.runs_dir / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(run_dir / "orchestrator.log"))

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Orkiestrator
# ─────────────────────────────────────────────

class Orchestrator:
    def __init__(self, project_root: Path = None):
        # Jeśli nie podano, przyjmij katalog nadrzędny wobec folderu taskmanager
        self.project_root = project_root or config.base_dir.parent
        self.repo = TaskRepository()
        self.claude = ClaudeAgent()
        self.gemini = GeminiAgent()
        self.git = GitHelper(self.project_root) if config.use_git else None
        
        logger.info(f"Project root set to: {self.project_root}")

    # ── Publiczny entry point ──

    def create_task(self, description: str) -> Task:
        task_id = f"TASK-{uuid.uuid4().hex[:6].upper()}"
        task = Task(task_id=task_id, description=description)
        self.repo.save(task)
        run_dir = config.runs_dir / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.md").write_text(
            f"# Task: {task_id}\n\n{description}\n", encoding="utf-8"
        )
        logger.info(f"Created task {task_id}")
        return task

    def run(self, task_id: str) -> Task:
        task = self.repo.load(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

        setup_logging(task_id)
        logger.info(f"═══ Starting task {task_id} (status={task.status}) ═══")

        while task.status not in (
            TaskStatus.APPROVED, TaskStatus.STUCK, TaskStatus.FAILED
        ):
            task = self._step(task)
            self.repo.save(task)
            self._write_artifacts(task)

        logger.info(f"═══ Task {task_id} finished with status={task.status} ═══")
        return task

    # ── Kroki flow ──

    def _step(self, task: Task) -> Task:
        if task.status == TaskStatus.NEW:
            return self._architecting(task)
        elif task.status in (TaskStatus.ARCHITECTING, TaskStatus.CHANGES_REQUESTED):
            return self._implementing(task)
        elif task.status == TaskStatus.IMPLEMENTING:
            return self._reviewing(task)
        else:
            logger.error(f"Unexpected status: {task.status}")
            task.status = TaskStatus.FAILED
            return task

    # ── FAZA 1: ARCHITECTING ──

    def _architecting(self, task: Task) -> Task:
        task.status = TaskStatus.ARCHITECTING
        logger.info(f"[{task.task_id}] Phase: ARCHITECTING")

        # Zapamiętaj SHA przed startem taska — do pełnego diffa w review
        if self.git and not task.task_start_sha:
            task.task_start_sha = self.git.get_current_sha()
            logger.info(f"Task start SHA: {task.task_start_sha[:8] or 'none'}")

        codebase = build_codebase_summary(self.project_root)
        prompt = architect_prompt(task.description, codebase)

        result = self.claude.call(prompt, expect_json=True)
        if not result.success:
            logger.error(f"Claude ARCHITECTING failed: {result.error}")
            task.status = TaskStatus.FAILED
            return task

        try:
            data = result.json()
        except ValueError as e:
            logger.error(f"ARCHITECTING — bad JSON: {e}")
            task.status = TaskStatus.FAILED
            return task

        # Zapisz plan
        task.architect_plan = json.dumps(data, ensure_ascii=False, indent=2)

        # Zapisz kryteria
        raw_criteria = data.get("acceptance_criteria", [])
        task.criteria = [
            {
                "id": c.get("id", f"c{i+1}"),
                "description": c.get("description", ""),
                "how_to_verify": c.get("how_to_verify", ""),
                "status": "PENDING",
                "evidence": None,
            }
            for i, c in enumerate(raw_criteria)
        ]

        logger.info(
            f"Plan created: {len(data.get('plan', []))} steps, "
            f"{len(task.criteria)} criteria"
        )
        task.status = TaskStatus.CHANGES_REQUESTED  # → wejdź w implementing
        return task

    # ── FAZA 2: IMPLEMENTING ──

    def _implementing(self, task: Task) -> Task:
        task.status = TaskStatus.IMPLEMENTING
        task.iteration += 1
        logger.info(
            f"[{task.task_id}] Phase: IMPLEMENTING (iteration {task.iteration})"
        )

        if task.iteration > task.max_iterations:
            logger.warning(f"Max iterations ({task.max_iterations}) reached → STUCK")
            task.status = TaskStatus.STUCK
            return task

        open_criteria = task.open_criteria_list()
        prompt = implement_prompt(
            task_description=task.description,
            architect_plan=task.architect_plan,
            open_criteria=open_criteria,
            previous_diff=task.last_diff,
            iteration=task.iteration,
        )

        run_dir = config.runs_dir / task.task_id
        result = self.gemini.call(prompt, cwd=self.project_root)
        if not result.success:
            logger.error(f"Gemini IMPLEMENTING failed: {result.error}")
            task.status = TaskStatus.FAILED
            return task

        # Sprawdź diff
        diff_stat, lines_changed = ("no git", 0)
        diff_full = ""
        if self.git:
            diff_stat, lines_changed = self.git.diff_stat()
            diff_full = self.git.full_diff()
            task.last_diff = diff_full
            logger.info(f"Git diff: {diff_stat} ({lines_changed} lines)")

            # Stuck detection
            if lines_changed < config.min_diff_lines:
                task.stuck_counter += 1
                logger.warning(
                    f"No meaningful diff! stuck_counter={task.stuck_counter}"
                )
                if task.stuck_counter >= config.max_stuck_rounds:
                    logger.error("STUCK — no progress after multiple iterations")
                    task.status = TaskStatus.STUCK
                    return task
            else:
                task.stuck_counter = 0

            # Auto-commit dla audytu
            if config.use_git:
                self.git.stage_and_commit(
                    f"[{task.task_id}] iter {task.iteration} — Gemini implementation"
                )

        task.status = TaskStatus.IMPLEMENTING  # → przejdź do review
        return task

    # ── FAZA 3: REVIEWING ──

    def _reviewing(self, task: Task) -> Task:
        logger.info(
            f"[{task.task_id}] Phase: REVIEWING (iteration {task.iteration})"
        )

        run_dir = config.runs_dir / task.task_id
        impl_report_path = self.project_root / "implementation_report.md"

        impl_report = ""
        if impl_report_path.exists():
            impl_report = impl_report_path.read_text(encoding="utf-8")

        # Pełny diff od startu taska (żeby Claude widział całość, nie tylko ostatnią iterację)
        if self.git and task.task_start_sha:
            diff = self.git.full_diff_from_sha(task.task_start_sha)
            logger.info(f"Review using full diff from SHA {task.task_start_sha[:8]}")
        else:
            diff = task.last_diff or "(no diff available)"

        prompt = review_prompt(
            task_description=task.description,
            criteria=task.criteria,
            implementation_report=impl_report,
            diff=diff,
            iteration=task.iteration,
        )

        result = self.claude.call(prompt, expect_json=True)
        if not result.success:
            logger.error(f"Claude REVIEWING failed: {result.error}")
            task.status = TaskStatus.FAILED
            return task

        try:
            review_data = result.json()
        except ValueError as e:
            logger.error(f"REVIEWING — bad JSON: {e}")
            task.status = TaskStatus.FAILED
            return task

        # Zaktualizuj kryteria na podstawie review
        criteria_results = review_data.get("criteria_results", [])
        criteria_map = {c["id"]: c for c in criteria_results}

        for criterion in task.criteria:
            cr = criteria_map.get(criterion["id"])
            if cr:
                criterion["status"] = cr.get("status", criterion["status"])
                criterion["evidence"] = cr.get("evidence")

        # Zapis historii iteracji
        open_ids = [c["id"] for c in task.open_criteria_list()]
        record = IterationRecord(
            iteration=task.iteration,
            diff_stat=task.last_diff[:200] if task.last_diff else "",
            diff_lines_changed=0,
            review_passed=review_data.get("overall_status") == "APPROVED",
            open_criteria=open_ids,
            notes=review_data.get("next_focus", ""),
        )
        task.history.append(record.to_dict())

        overall = review_data.get("overall_status", "CHANGES_REQUESTED")
        blocking = review_data.get("blocking_issues", [])

        logger.info(
            f"Review result: {overall}, "
            f"open criteria: {open_ids}, "
            f"blocking issues: {len(blocking)}"
        )

        if overall == "APPROVED" and task.all_criteria_done():
            task.status = TaskStatus.APPROVED
        else:
            task.status = TaskStatus.CHANGES_REQUESTED

        # Zapisz review do pliku
        review_path = run_dir / f"review_iter_{task.iteration}.json"
        review_path.write_text(
            json.dumps(review_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        return task

    # ── Zapis artefaktów ──

    def _write_artifacts(self, task: Task) -> None:
        run_dir = config.runs_dir / task.task_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Stan jako JSON
        (run_dir / "state.json").write_text(task.to_json(), encoding="utf-8")

        # Plan architekta (jeśli jest)
        if task.architect_plan:
            try:
                plan_data = json.loads(task.architect_plan)
                (run_dir / "architect_plan.json").write_text(
                    json.dumps(plan_data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass

        logger.debug(f"Artifacts written to {run_dir}")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def cmd_new(description: str) -> None:
    orch = Orchestrator()
    task = orch.create_task(description)
    print(f"\n✅ Task created: {task.task_id}")
    print(f"   Run with: python runner.py run {task.task_id}\n")


def cmd_run(task_id: str) -> None:
    orch = Orchestrator()
    try:
        task = orch.run(task_id)
    except ValueError as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)

    status_icons = {
        TaskStatus.APPROVED: "✅",
        TaskStatus.STUCK: "⚠️",
        TaskStatus.FAILED: "❌",
    }
    icon = status_icons.get(task.status, "❓")
    print(f"\n{icon} Task {task.task_id} finished: {task.status.value}")
    print(f"   Iterations: {task.iteration}")

    if task.status == TaskStatus.APPROVED:
        done = sum(1 for c in task.criteria if c["status"] == "DONE")
        print(f"   Criteria: {done}/{len(task.criteria)} done")
    elif task.status in (TaskStatus.STUCK, TaskStatus.FAILED):
        print(f"   Open criteria: {[c['id'] for c in task.open_criteria_list()]}")
        print(f"   → Check runs/{task_id}/ for details")


def cmd_status(task_id: str = None) -> None:
    repo = TaskRepository()
    if task_id:
        task = repo.load(task_id)
        if not task:
            print(f"Task {task_id} not found")
            return
        tasks = [task]
    else:
        tasks = repo.list_all()

    if not tasks:
        print("No tasks found.")
        return

    print(f"\n{'ID':<15} {'STATUS':<22} {'ITER':<6} {'CRITERIA':<12}")
    print("─" * 60)
    for t in tasks:
        done = sum(1 for c in t.criteria if c["status"] == "DONE")
        total = len(t.criteria)
        crit_str = f"{done}/{total}" if total else "—"
        print(f"{t.task_id:<15} {t.status.value:<22} {t.iteration:<6} {crit_str:<12}")
    print()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main() -> None:
    override_from_env()
    config.runs_dir.mkdir(parents=True, exist_ok=True)
    setup_logging()

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]

    if cmd == "new":
        if len(args) < 2:
            print("Usage: python runner.py new '<task description>'")
            sys.exit(1)
        cmd_new(" ".join(args[1:]))

    elif cmd == "run":
        if len(args) < 2:
            print("Usage: python runner.py run <TASK-ID>")
            sys.exit(1)
        cmd_run(args[1])

    elif cmd == "status":
        cmd_status(args[1] if len(args) > 1 else None)

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: new | run | status")
        sys.exit(1)


if __name__ == "__main__":
    main()
