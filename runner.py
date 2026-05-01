"""
runner.py — główna pętla orkiestratora

Użycie:
    orch new "Zrefaktoruj moduł parsera do nowej architektury"
    orch run TASK-001
    orch status
    orch status TASK-001
"""

import json
import logging
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from agents import ClaudeAgent, GeminiAgent, GitHelper, create_agent
from config import config, override_from_env
from prompts import (
    architect_prompt,
    build_codebase_summary,
    code_quality_review_prompt,
    human_feedback_prompt,
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
# Conversation logger
# ─────────────────────────────────────────────

class ConversationLogger:
    """Zapisuje przebieg konwersacji do conversation.md."""

    def __init__(
        self,
        run_dir: Path,
        task_id: str,
        description: str,
        architect_name: str = "Claude",
        developer_name: str = "Gemini",
        reviewer_name: str = "Claude",
    ):
        self.architect_name = architect_name.capitalize()
        self.developer_name = developer_name.capitalize()
        self.reviewer_name = reviewer_name.capitalize()
        self.path = run_dir / "conversation.md"
        if not self.path.exists():
            self.path.write_text(
                f"# Conversation Log — {task_id}\n\n"
                f"**Task:** {description}\n\n"
                f"**Roles:** architect={self.architect_name}, "
                f"developer={self.developer_name}, reviewer={self.reviewer_name}\n\n"
                f"---\n\n",
                encoding="utf-8",
            )

    def _append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(text)

    def _ts(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log_architecting(self, data: dict) -> None:
        plan = data.get("plan", [])
        criteria = data.get("acceptance_criteria", [])
        risks = data.get("risks", [])

        plan_lines = "\n".join(
            f"{s.get('step', i + 1)}. **{s.get('title', '')}** — "
            f"{s.get('description', '')} `[{s.get('type', '')}]`"
            for i, s in enumerate(plan)
        )
        criteria_lines = "\n".join(
            f"- `[{c.get('id', '')}]` {c.get('description', '')}  \n"
            f"  *Verify:* {c.get('how_to_verify', '')}"
            for c in criteria
        )
        risks_lines = "\n".join(f"- {r}" for r in risks) or "*None*"

        self._append(
            f"## ARCHITECTING — {self._ts()}\n\n"
            f"### {self.architect_name} (Architekt)\n\n"
            f"**Summary:** {data.get('summary', '')}\n\n"
            f"**Plan:**\n{plan_lines}\n\n"
            f"**Acceptance Criteria:**\n{criteria_lines}\n\n"
            f"**Risks:**\n{risks_lines}\n\n"
            f"---\n\n"
        )

    def log_analyzing(self, data: dict) -> None:
        plan = data.get("plan", [])

        plan_lines = "\n".join(
            f"{s.get('step', i + 1)}. **{s.get('title', '')}** — "
            f"{s.get('description', '')} `[{s.get('type', '')}]`  \n"
            f"   *Symbols:* `{', '.join(s.get('symbols_affected', []))}`  \n"
            f"   *Hints:* {s.get('code_hints', '')}"
            for i, s in enumerate(plan)
        )

        self._append(
            f"## ANALYZING — {self._ts()}\n\n"
            f"### Gemini (Analityk Kodu)\n\n"
            f"**Enriched Plan:**\n\n{plan_lines}\n\n"
            f"---\n\n"
        )

    def log_implementing(
        self,
        iteration: int,
        open_criteria: list,
        fix_context: str,
        diff_stat: str,
        impl_report: str,
        developer_output: str,
    ) -> None:
        if fix_context:
            context_block = f"**Fix context przekazany {self.developer_name}:**\n```\n{fix_context}\n```\n\n"
        elif open_criteria:
            items = "\n".join(
                f"- `[{c['id']}]` {c['description']}" for c in open_criteria
            )
            context_block = f"**Open criteria do adresowania:**\n{items}\n\n"
        else:
            context_block = "*Pierwsza iteracja — implementacja pełnego planu.*\n\n"

        report_block = (
            impl_report.strip()
            if impl_report.strip()
            else "*Brak implementation_report.md.*"
        )

        developer_block = ""
        if developer_output.strip():
            snippet = developer_output[:2000]
            if len(developer_output) > 2000:
                snippet += "\n*(truncated)*"
            developer_block = (
                f"<details>\n<summary>{self.developer_name} stdout (raw)</summary>\n\n"
                f"```\n{snippet}\n```\n</details>\n\n"
            )

        self._append(
            f"## IMPLEMENTING — iter {iteration} — {self._ts()}\n\n"
            f"### {self.developer_name} (Programista)\n\n"
            f"{context_block}"
            f"**Git diff:** {diff_stat}\n\n"
            f"**Implementation Report:**\n\n{report_block}\n\n"
            f"{developer_block}"
            f"---\n\n"
        )

    def log_reviewing(self, iteration: int, data: dict, human_review: bool = False) -> None:
        overall = data.get("overall_status", "?")
        overall_icon = "✅" if overall == "APPROVED" else "🔄"

        icon = {"DONE": "✅", "PENDING": "⏳", "FAILED": "❌"}
        criteria_lines = "\n".join(
            f"- {icon.get(c.get('status', ''), '?')} `[{c.get('id', '')}]` "
            f"**{c.get('status', '')}** — {c.get('evidence', '')}  \n"
            f"  *confidence:* {c.get('confidence', '?')}"
            for c in data.get("criteria_results", [])
        )

        blocking = data.get("blocking_issues", [])
        blocking_block = "\n".join(f"- 🚫 {b}" for b in blocking) if blocking else "*None*"

        suggestions = data.get("suggestions", [])
        suggestions_block = (
            "\n".join(f"- 💡 {s}" for s in suggestions) if suggestions else "*None*"
        )

        next_focus = data.get("next_focus", "")
        mode_note = " *(human-approved — tylko jakość kodu)*" if human_review else ""

        self._append(
            f"## REVIEWING — iter {iteration} — {self._ts()}{mode_note}\n\n"
            f"### {self.reviewer_name} (Reviewer)\n\n"
            f"**Overall:** {overall_icon} {overall}\n\n"
            f"**Criteria:**\n{criteria_lines}\n\n"
            f"**Blocking issues:**\n{blocking_block}\n\n"
            f"**Suggestions:**\n{suggestions_block}\n\n"
            + (f"**Next focus:** {next_focus}\n\n" if next_focus else "")
            + "---\n\n"
        )

    def log_awaiting_human(self, iteration: int) -> None:
        self._append(
            f"## AWAITING_HUMAN — iter {iteration} — {self._ts()}\n\n"
            f"*Orkiestrator czeka na decyzję człowieka...*\n\n"
        )

    def log_human_decision(self, iteration: int, approved: bool, feedback: str = "") -> None:
        if approved:
            self._append(
                f"**Decyzja człowieka:** ✅ OK — implementacja działa poprawnie\n\n"
                f"---\n\n"
            )
        else:
            self._append(
                f"**Decyzja człowieka:** ❌ FAIL\n\n"
                f"**Feedback:** {feedback}\n\n"
                f"---\n\n"
            )

    def log_human_feedback(self, iteration: int, data: dict) -> None:
        fix_steps = data.get("fix_steps", [])
        steps_lines = "\n".join(
            f"{s.get('step', i + 1)}. {s.get('description', '')}  \n"
            f"   *files:* {', '.join(s.get('files_affected', []))}"
            for i, s in enumerate(fix_steps)
        )

        self._append(
            f"## HUMAN_FEEDBACK — iter {iteration} — {self._ts()}\n\n"
            f"### {self.reviewer_name} (Analiza feedbacku → plan naprawy)\n\n"
            f"**Root cause:** {data.get('root_cause', '')}\n\n"
            f"**Fix steps:**\n{steps_lines}\n\n"
            f"**Key fix:** {data.get('key_fix', '')}\n\n"
            f"---\n\n"
        )


# ─────────────────────────────────────────────
# Orkiestrator
# ─────────────────────────────────────────────

class Orchestrator:
    def __init__(self, project_root: Path = None, human_review: bool = False):
        # Jeśli nie podano, przyjmij wykryty root projektu (git root lub CWD)
        self.project_root = project_root or config.base_dir
        self.repo = TaskRepository()
        self.architect = create_agent(config.architect_role)
        self.analyzer = create_agent(config.analyzer_role)
        self.developer = create_agent(config.developer_role)
        self.reviewer = create_agent(config.reviewer_role)
        self.git = GitHelper(self.project_root) if config.use_git else None
        self.human_review = human_review
        self.conv_log: ConversationLogger | None = None

        logger.info(
            f"Project root set to: {self.project_root} | "
            f"roles: architect={config.architect_role}, "
            f"developer={config.developer_role}, reviewer={config.reviewer_role}"
        )
        if human_review:
            logger.info("Human-review mode enabled")

    # ── Publiczny entry point ──

    def create_task(self, description: str) -> Task:
        task_id = f"TASK-{uuid.uuid4().hex[:6].upper()}"
        # Wyciągnij pierwsze 100 znaków pierwszej linii jako tytuł
        first_line = description.split("\n")[0].strip()
        title = first_line[:100]
        task = Task(task_id=task_id, description=description, title=title)
        self.repo.save(task)
        run_dir = config.runs_dir / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "task.md").write_text(
            f"# Task: {task_id}\n\n## Title: {title}\n\n{description}\n", encoding="utf-8"
        )
        logger.info(f"Created task {task_id} with title: {title}")
        return task

    def run(self, task_id: str) -> Task:
        task = self.repo.load(task_id)
        if task is None:
            raise ValueError(f"Task {task_id} not found")

        setup_logging(task_id)
        logger.info(f"═══ Starting task {task_id} (status={task.status}) ═══")

        run_dir = config.runs_dir / task_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.conv_log = ConversationLogger(
            run_dir, task_id, task.description,
            architect_name=config.architect_role,
            developer_name=config.developer_role,
            reviewer_name=config.reviewer_role,
        )

        # Jeśli startujemy z feedbackiem (np. po orch follow), zaloguj go od razu
        if task.status == TaskStatus.HUMAN_FEEDBACK and task.human_feedback:
            # Tworzymy sztuczną strukturę dla loggera, żeby wiedział co zapisać
            fake_data = {
                "root_cause": "Follow-up request initiated by user",
                "fix_steps": [{"step": 1, "description": task.human_feedback, "files_affected": []}],
                "key_fix": task.human_feedback
            }
            # Logujemy to jako HUMAN_FEEDBACK w konwersacji
            self.conv_log._append(f"## FOLLOW-UP INITIATED — {self.conv_log._ts()}\n\n")
            self.conv_log._append(f"**User instruction:** {task.human_feedback}\n\n---\n\n")

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
        elif task.status == TaskStatus.ARCHITECTING:
            return self._analyzing(task)
        elif task.status in (TaskStatus.ANALYZING, TaskStatus.CHANGES_REQUESTED):
            return self._implementing(task)
        elif task.status in (TaskStatus.IMPLEMENTING, TaskStatus.REVIEWING):
            return self._reviewing(task)
        elif task.status == TaskStatus.AWAITING_HUMAN:
            return self._awaiting_human(task)
        elif task.status == TaskStatus.HUMAN_FEEDBACK:
            return self._human_feedback(task)
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

        result = self.architect.call(prompt, expect_json=True)
        if not result.success:
            logger.error(f"[{config.architect_role}] ARCHITECTING failed: {result.error}")
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

        if self.conv_log:
            self.conv_log.log_architecting(data)

        task.status = TaskStatus.ANALYZING  # → wejdź w analyzing
        return task

    # ── FAZA 1b: ANALYZING — Gemini bada kod i wzbogaca plan ──

    def _analyzing(self, task: Task) -> Task:
        logger.info(f"[{task.task_id}] Phase: ANALYZING")

        codebase = build_codebase_summary(self.project_root)
        from prompts import analyze_prompt
        prompt = analyze_prompt(task.description, task.architect_plan, codebase)

        result = self.analyzer.call(prompt, cwd=self.project_root, expect_json=True)
        if not result.success:
            logger.error(f"[{config.analyzer_role}] ANALYZING failed: {result.error}")
            task.status = TaskStatus.FAILED
            return task

        try:
            data = result.json()
        except ValueError as e:
            logger.error(f"ANALYZING — bad JSON: {e}")
            task.status = TaskStatus.FAILED
            return task

        # Nadpisz plan nowym (wzbogaconym)
        try:
            old_data = json.loads(task.architect_plan)
            old_data["plan"] = data.get("plan", [])
            task.architect_plan = json.dumps(old_data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Could not merge enriched plan: {e}")

        if self.conv_log:
            self.conv_log.log_analyzing(data)

        task.status = TaskStatus.IMPLEMENTING
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
        fix_context = task.fix_plan
        task.fix_plan = ""  # jednorazowe użycie
        prompt = implement_prompt(
            task_description=task.description,
            architect_plan=task.architect_plan,
            open_criteria=open_criteria,
            previous_diff=task.last_diff,
            iteration=task.iteration,
            fix_context=fix_context,
        )

        run_dir = config.runs_dir / task.task_id
        result = self.developer.call(prompt, cwd=self.project_root, expect_json=False)
        if not result.success:
            logger.error(f"[{config.developer_role}] IMPLEMENTING failed: {result.error}")
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
                    f"[{task.task_id}] iter {task.iteration} — {config.developer_role} implementation"
                )

        if self.conv_log:
            impl_report_path = self.project_root / "implementation_report.md"
            impl_report = (
                impl_report_path.read_text(encoding="utf-8")
                if impl_report_path.exists()
                else ""
            )
            self.conv_log.log_implementing(
                iteration=task.iteration,
                open_criteria=open_criteria,
                fix_context=fix_context,
                diff_stat=diff_stat,
                impl_report=impl_report,
                developer_output=result.raw_output,
            )

        if self.human_review:
            task.status = TaskStatus.AWAITING_HUMAN
        else:
            task.status = TaskStatus.IMPLEMENTING  # → przejdź do review
        return task

    # ── FAZA 3a: AWAITING_HUMAN ──

    def _awaiting_human(self, task: Task) -> Task:
        task.status = TaskStatus.AWAITING_HUMAN
        self.repo.save(task)
        self._write_artifacts(task)

        if self.conv_log:
            self.conv_log.log_awaiting_human(task.iteration)

        print(f"\n{'─'*60}")
        print(f"  ⏸  HUMAN REVIEW REQUIRED — iter {task.iteration}")
        print(f"  Task: {task.task_id}")
        print(f"  Uruchom aplikację i sprawdź czy działa poprawnie.")
        print(f"{'─'*60}\n")

        while True:
            answer = input("  Czy działa poprawnie? [ok / fail]: ").strip().lower()

            if answer == "ok":
                task.human_feedback = ""
                task.status = TaskStatus.REVIEWING
                if self.conv_log:
                    self.conv_log.log_human_decision(task.iteration, approved=True)
                break

            elif answer == "fail":
                feedback = input("  Co nie działa? Opisz konkretnie: ").strip()
                if not feedback:
                    print("  Podaj opis problemu.")
                    continue
                task.human_feedback = feedback
                task.status = TaskStatus.HUMAN_FEEDBACK
                if self.conv_log:
                    self.conv_log.log_human_decision(
                        task.iteration, approved=False, feedback=feedback
                    )
                break

            else:
                print("  Wpisz 'ok' lub 'fail'.")

        self.repo.save(task)
        return task

    # ── FAZA 3b: HUMAN_FEEDBACK — Claude analizuje feedback i tworzy plan naprawy ──

    def _human_feedback(self, task: Task) -> Task:
        logger.info(
            f"[{task.task_id}] Phase: HUMAN_FEEDBACK (iteration {task.iteration})"
        )

        impl_report_path = self.project_root / "implementation_report.md"
        impl_report = ""
        if impl_report_path.exists():
            impl_report = impl_report_path.read_text(encoding="utf-8")

        if self.git and task.task_start_sha:
            diff = self.git.full_diff_from_sha(task.task_start_sha)
        else:
            diff = task.last_diff or "(no diff available)"

        prompt = human_feedback_prompt(
            task_description=task.description,
            architect_plan=task.architect_plan,
            human_feedback=task.human_feedback,
            implementation_report=impl_report,
            diff=diff,
            iteration=task.iteration,
        )

        result = self.reviewer.call(prompt, expect_json=True)
        if not result.success:
            logger.error(f"[{config.reviewer_role}] HUMAN_FEEDBACK failed: {result.error}")
            task.status = TaskStatus.FAILED
            return task

        try:
            fix_data = result.json()
        except ValueError as e:
            logger.error(f"HUMAN_FEEDBACK — bad JSON: {e}")
            task.status = TaskStatus.FAILED
            return task

        fix_steps = fix_data.get("fix_steps", [])
        steps_text = "\n".join(
            f"  {s['step']}. {s['description']} "
            f"(files: {', '.join(s.get('files_affected', []))})"
            for s in fix_steps
        )
        task.fix_plan = (
            f"Root cause: {fix_data.get('root_cause', '')}\n\n"
            f"Fix steps:\n{steps_text}\n\n"
            f"Key fix: {fix_data.get('key_fix', '')}"
        )

        # Nowy feedback = nowa informacja → reset stuck counter
        task.stuck_counter = 0
        logger.info(
            f"Fix plan created, stuck_counter reset. "
            f"Key fix: {fix_data.get('key_fix', '')}"
        )

        if self.conv_log:
            self.conv_log.log_human_feedback(task.iteration, fix_data)

        task.status = TaskStatus.CHANGES_REQUESTED
        return task

    # ── FAZA 4: REVIEWING ──

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

        if self.human_review:
            prompt = code_quality_review_prompt(
                task_description=task.description,
                criteria=task.criteria,
                implementation_report=impl_report,
                diff=diff,
                iteration=task.iteration,
            )
        else:
            prompt = review_prompt(
                task_description=task.description,
                criteria=task.criteria,
                implementation_report=impl_report,
                diff=diff,
                iteration=task.iteration,
            )

        result = self.reviewer.call(prompt, expect_json=True)
        if not result.success:
            logger.error(f"[{config.reviewer_role}] REVIEWING failed: {result.error}")
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

        if self.conv_log:
            self.conv_log.log_reviewing(
                task.iteration, review_data, human_review=self.human_review
            )

        if overall == "APPROVED" and task.all_criteria_done():
            task.status = TaskStatus.APPROVED
        else:
            # W trybie human_review blocking issues z code review → fix_plan dla Gemini
            if self.human_review and blocking:
                next_focus = review_data.get("next_focus", "")
                task.fix_plan = (
                    "Code quality issues from review:\n"
                    + "\n".join(f"- {issue}" for issue in blocking)
                    + (f"\n\nNext focus: {next_focus}" if next_focus else "")
                )
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
    print(f"   Run with: orch run {task.task_id}\n")


def cmd_run(task_id: str, human_review: bool = False) -> None:
    orch = Orchestrator(human_review=human_review)
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


def cmd_reset(task_id: str) -> None:
    repo = TaskRepository()
    task = repo.load(task_id)
    if not task:
        print(f"Task {task_id} not found")
        sys.exit(1)

    old_status = task.status.value
    task.status = TaskStatus.NEW
    task.iteration = 0
    task.stuck_counter = 0
    task.criteria = []
    task.history = []
    task.architect_plan = ""
    task.last_diff = ""
    task.task_start_sha = ""
    task.human_feedback = ""
    task.fix_plan = ""
    repo.save(task)

    # Wyczyść artefakty poza task.md
    run_dir = config.runs_dir / task_id
    for f in ("architect_plan.json", "state.json", "conversation.md"):
        p = run_dir / f
        if p.exists():
            p.unlink()
    for f in run_dir.glob("review_iter_*.json"):
        f.unlink()

    print(f"\n🔄 Task {task_id} zresetowany: {old_status} → NEW")
    print(f"   Run with: orch run {task_id}\n")


def cmd_follow(task_id: str, feedback: str) -> None:
    repo = TaskRepository()
    task = repo.load(task_id)
    if not task:
        print(f"Task {task_id} not found")
        sys.exit(1)

    task.human_feedback = f"Follow-up request: {feedback}"
    task.status = TaskStatus.HUMAN_FEEDBACK
    task.stuck_counter = 0
    repo.save(task)

    print(f"\n✅ Task {task_id} wznowiony jako kontynuacja (status: HUMAN_FEEDBACK)")
    print(f"   Dodano polecenie: {feedback}")
    print(f"   Run with: orch run {task_id}\n")


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

    print(f"\n{'ID':<15} {'TITLE':<42} {'STATUS':<22} {'ITER':<6} {'CRITERIA':<12}")
    print("─" * 105)
    for t in tasks:
        done = sum(1 for c in t.criteria if c["status"] == "DONE")
        total = len(t.criteria)
        crit_str = f"{done}/{total}" if total else "—"
        title = t.title[:40] + "..." if len(t.title) > 40 else t.title
        print(f"{t.task_id:<15} {title:<42} {t.status.value:<22} {t.iteration:<6} {crit_str:<12}")
    print()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def _apply_role_flags(args: list[str]) -> None:
    """Parse --architect=X --developer=X --reviewer=X flags and update config."""
    for arg in args:
        if arg.startswith("--architect="):
            config.architect_role = arg.split("=", 1)[1]
        elif arg.startswith("--developer="):
            config.developer_role = arg.split("=", 1)[1]
        elif arg.startswith("--reviewer="):
            config.reviewer_role = arg.split("=", 1)[1]


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
            print("Usage: orch new '<task description>'")
            sys.exit(1)
        cmd_new(" ".join(a for a in args[1:] if not a.startswith("--")))

    elif cmd == "run":
        run_args = [a for a in args[1:] if not a.startswith("--")]
        human_review = "--human-review" in args
        _apply_role_flags(args)
        if not run_args:
            print(
                "Usage: orch run <TASK-ID> [--human-review] "
                "[--architect=claude|gemini] [--developer=claude|gemini] "
                "[--reviewer=claude|gemini]"
            )
            sys.exit(1)
        cmd_run(run_args[0], human_review=human_review)

    elif cmd == "status":
        cmd_status(args[1] if len(args) > 1 else None)

    elif cmd == "reset":
        if len(args) < 2:
            print("Usage: orch reset <TASK-ID>")
            sys.exit(1)
        cmd_reset(args[1])

    elif cmd == "follow":
        if len(args) < 3:
            print("Usage: orch follow <TASK-ID> '<feedback/instructions>'")
            sys.exit(1)
        cmd_follow(args[1], " ".join(args[2:]))

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: new | run | status | reset | follow")
        sys.exit(1)


if __name__ == "__main__":
    main()
