"""
tests/test_orchestrator.py — testy jednostkowe i integracyjne

Uruchom: pytest tests/ -v
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents import AgentResult, BaseAgent, GitHelper
from config import OrchestratorConfig, config
from prompts import architect_prompt, implement_prompt, review_prompt
from runner import Orchestrator
from state import Criterion, IterationRecord, Task, TaskRepository, TaskStatus


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

@pytest.fixture
def tmp_dir(tmp_path):
    """Tymczasowy katalog dla każdego testu."""
    return tmp_path


@pytest.fixture
def tmp_config(tmp_dir):
    """Config z nadpisanymi ścieżkami do tmp."""
    config.runs_dir = tmp_dir / "runs"
    config.db_path = tmp_dir / "test.db"
    config.use_git = False
    config.max_iterations = 3
    config.agent_max_retries = 1
    yield config
    # Reset po teście
    config.runs_dir = Path("runs")
    config.db_path = Path("orchestrator.db")
    config.use_git = True
    config.max_iterations = 6
    config.agent_max_retries = 3


@pytest.fixture
def repo(tmp_config, tmp_dir):
    return TaskRepository(tmp_dir / "test.db")


@pytest.fixture
def sample_task():
    return Task(
        task_id="TASK-TEST01",
        description="Refaktoryzuj moduł parsera",
    )


# ─────────────────────────────────────────────
# Testy: State / TaskRepository
# ─────────────────────────────────────────────

class TestTaskRepository:

    def test_save_and_load(self, repo, sample_task):
        repo.save(sample_task)
        loaded = repo.load("TASK-TEST01")
        assert loaded is not None
        assert loaded.task_id == "TASK-TEST01"
        assert loaded.status == TaskStatus.NEW

    def test_load_nonexistent(self, repo):
        assert repo.load("TASK-DOESNOTEXIST") is None

    def test_update_status(self, repo, sample_task):
        repo.save(sample_task)
        sample_task.status = TaskStatus.APPROVED
        repo.save(sample_task)
        loaded = repo.load("TASK-TEST01")
        assert loaded.status == TaskStatus.APPROVED

    def test_list_all(self, repo):
        t1 = Task(task_id="TASK-A", description="Task A")
        t2 = Task(task_id="TASK-B", description="Task B")
        repo.save(t1)
        repo.save(t2)
        all_tasks = repo.list_all()
        assert len(all_tasks) == 2

    def test_list_by_status(self, repo):
        t1 = Task(task_id="TASK-A", description="A", status=TaskStatus.APPROVED)
        t2 = Task(task_id="TASK-B", description="B", status=TaskStatus.STUCK)
        repo.save(t1)
        repo.save(t2)
        approved = repo.list_by_status(TaskStatus.APPROVED)
        assert len(approved) == 1
        assert approved[0].task_id == "TASK-A"

    def test_serialization_roundtrip(self, repo):
        task = Task(
            task_id="TASK-SER",
            description="Test serialization",
            status=TaskStatus.REVIEWING,
            iteration=3,
            criteria=[{"id": "c1", "description": "Test", "status": "DONE", "evidence": "line 42"}],
            history=[{"iteration": 1, "diff_stat": "+10/-2", "diff_lines_changed": 12,
                       "review_passed": False, "open_criteria": ["c1"], "notes": "fix it",
                       "timestamp": time.time()}],
        )
        repo.save(task)
        loaded = repo.load("TASK-SER")
        assert loaded.iteration == 3
        assert loaded.status == TaskStatus.REVIEWING
        assert len(loaded.criteria) == 1
        assert loaded.criteria[0]["id"] == "c1"


# ─────────────────────────────────────────────
# Testy: Task model
# ─────────────────────────────────────────────

class TestTaskModel:

    def test_all_criteria_done_empty(self):
        task = Task(task_id="X", description="X")
        assert task.all_criteria_done() is False

    def test_all_criteria_done_true(self):
        task = Task(task_id="X", description="X")
        task.criteria = [
            {"id": "c1", "status": "DONE"},
            {"id": "c2", "status": "DONE"},
        ]
        assert task.all_criteria_done() is True

    def test_all_criteria_done_false(self):
        task = Task(task_id="X", description="X")
        task.criteria = [
            {"id": "c1", "status": "DONE"},
            {"id": "c2", "status": "PENDING"},
        ]
        assert task.all_criteria_done() is False

    def test_open_criteria_list(self):
        task = Task(task_id="X", description="X")
        task.criteria = [
            {"id": "c1", "status": "DONE"},
            {"id": "c2", "status": "PENDING"},
            {"id": "c3", "status": "FAILED"},
        ]
        open_c = task.open_criteria_list()
        assert len(open_c) == 2
        assert open_c[0]["id"] == "c2"


# ─────────────────────────────────────────────
# Testy: AgentResult / JSON parsing
# ─────────────────────────────────────────────

class TestAgentJsonParsing:

    def setup_method(self):
        self.agent = BaseAgent()

    def test_clean_json(self):
        result = self.agent._parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_with_markdown_fence(self):
        raw = "```json\n{\"key\": \"value\"}\n```"
        result = self.agent._parse_json(raw)
        assert result == {"key": "value"}

    def test_json_with_preamble(self):
        raw = 'Here is my response:\n\n{"status": "APPROVED"}'
        result = self.agent._parse_json(raw)
        assert result == {"status": "APPROVED"}

    def test_invalid_json_returns_none(self):
        result = self.agent._parse_json("This is not JSON at all")
        assert result is None

    def test_json_array(self):
        result = self.agent._parse_json('[{"id": 1}, {"id": 2}]')
        assert isinstance(result, list)
        assert len(result) == 2

    def test_nested_json(self):
        raw = '{"criteria": [{"id": "c1", "status": "DONE"}]}'
        result = self.agent._parse_json(raw)
        assert result["criteria"][0]["status"] == "DONE"


# ─────────────────────────────────────────────
# Testy: Prompts
# ─────────────────────────────────────────────

class TestPrompts:

    def test_architect_prompt_contains_task(self):
        prompt = architect_prompt("Refactor parser module", "## File tree\n```\nparser.py\n```")
        assert "Refactor parser module" in prompt
        assert "parser.py" in prompt
        assert "acceptance_criteria" in prompt

    def test_implement_prompt_first_iteration(self):
        prompt = implement_prompt(
            task_description="Add logging",
            architect_plan='{"plan": []}',
            open_criteria=[],
            previous_diff="",
            iteration=1,
        )
        assert "Iteration: 1" in prompt
        assert "implementation_report.md" in prompt

    def test_implement_prompt_with_open_criteria(self):
        open_c = [{"id": "c2", "description": "Function X must exist"}]
        prompt = implement_prompt(
            task_description="Fix stuff",
            architect_plan="{}",
            open_criteria=open_c,
            iteration=2,
        )
        assert "c2" in prompt
        assert "Function X must exist" in prompt
        assert "Unfinished criteria" in prompt

    def test_review_prompt_structure(self):
        criteria = [{"id": "c1", "description": "Test passes", "status": "PENDING"}]
        prompt = review_prompt(
            task_description="Add tests",
            criteria=criteria,
            implementation_report="## Changes\n- Added test_foo.py",
            diff="+def test_foo():\n+    assert True",
            iteration=1,
        )
        assert "APPROVED" in prompt
        assert "CHANGES_REQUESTED" in prompt
        assert "c1" in prompt
        assert "evidence" in prompt


# ─────────────────────────────────────────────
# Testy integracyjne: full flow z mock agents
# ─────────────────────────────────────────────

MOCK_ARCHITECT_RESPONSE = json.dumps({
    "summary": "Refactor the parser",
    "plan": [{"step": 1, "title": "Update parser.py", "description": "Add new method",
               "files_affected": ["parser.py"], "type": "MODIFY"}],
    "acceptance_criteria": [
        {"id": "c1", "description": "Function parse_v2 exists in parser.py",
         "how_to_verify": "grep 'def parse_v2' parser.py"},
        {"id": "c2", "description": "All tests pass",
         "how_to_verify": "pytest returns 0"},
    ],
    "risks": [],
})

MOCK_REVIEW_APPROVED = json.dumps({
    "iteration": 1,
    "overall_status": "APPROVED",
    "criteria_results": [
        {"id": "c1", "description": "Function parse_v2 exists",
         "status": "DONE", "evidence": "line 42: def parse_v2()", "confidence": "HIGH"},
        {"id": "c2", "description": "All tests pass",
         "status": "DONE", "evidence": "diff shows no test failures", "confidence": "MEDIUM"},
    ],
    "blocking_issues": [],
    "suggestions": [],
    "next_focus": "",
})

MOCK_REVIEW_CHANGES = json.dumps({
    "iteration": 1,
    "overall_status": "CHANGES_REQUESTED",
    "criteria_results": [
        {"id": "c1", "description": "Function parse_v2 exists",
         "status": "DONE", "evidence": "line 42: def parse_v2()", "confidence": "HIGH"},
        {"id": "c2", "description": "All tests pass",
         "status": "PENDING", "evidence": "no test file in diff", "confidence": "LOW"},
    ],
    "blocking_issues": ["Tests not implemented"],
    "suggestions": [],
    "next_focus": "Add test_parser.py with tests for parse_v2",
})


class TestOrchestratorFlow:

    def _make_orch(self, tmp_config, tmp_dir):
        orch = Orchestrator(project_root=tmp_dir)
        orch.repo = TaskRepository(tmp_dir / "test.db")
        return orch

    def test_create_task(self, tmp_config, tmp_dir):
        orch = self._make_orch(tmp_config, tmp_dir)
        task = orch.create_task("Test task description")
        assert task.task_id.startswith("TASK-")
        assert task.status == TaskStatus.NEW
        assert (tmp_config.runs_dir / task.task_id / "task.md").exists()

    def test_full_flow_approved_first_try(self, tmp_config, tmp_dir):
        orch = self._make_orch(tmp_config, tmp_dir)

        # Mock Claude: architect → approved review
        claude_responses = [
            AgentResult(success=True, raw_output=MOCK_ARCHITECT_RESPONSE,
                        parsed=json.loads(MOCK_ARCHITECT_RESPONSE)),
            AgentResult(success=True, raw_output=MOCK_REVIEW_APPROVED,
                        parsed=json.loads(MOCK_REVIEW_APPROVED)),
        ]
        claude_call_count = [0]
        def mock_claude_call(prompt, **kwargs):
            r = claude_responses[claude_call_count[0]]
            claude_call_count[0] += 1
            return r
        orch.claude.call = mock_claude_call

        # Mock Gemini
        orch.gemini.call = MagicMock(return_value=AgentResult(
            success=True, raw_output="Done"
        ))

        task = orch.create_task("Refactor parser")
        task = orch.run(task.task_id)

        assert task.status == TaskStatus.APPROVED
        assert task.iteration == 1
        assert task.all_criteria_done()

    def test_flow_changes_then_approved(self, tmp_config, tmp_dir):
        orch = self._make_orch(tmp_config, tmp_dir)

        claude_responses = [
            AgentResult(success=True, raw_output=MOCK_ARCHITECT_RESPONSE,
                        parsed=json.loads(MOCK_ARCHITECT_RESPONSE)),
            AgentResult(success=True, raw_output=MOCK_REVIEW_CHANGES,
                        parsed=json.loads(MOCK_REVIEW_CHANGES)),
            AgentResult(success=True, raw_output=MOCK_REVIEW_APPROVED,
                        parsed=json.loads(MOCK_REVIEW_APPROVED)),
        ]
        idx = [0]
        def mock_claude(prompt, **kwargs):
            r = claude_responses[idx[0]]
            idx[0] += 1
            return r
        orch.claude.call = mock_claude
        orch.gemini.call = MagicMock(return_value=AgentResult(success=True, raw_output="Done"))

        task = orch.create_task("Refactor parser with tests")
        task = orch.run(task.task_id)

        assert task.status == TaskStatus.APPROVED
        assert task.iteration == 2

    def test_flow_max_iterations_stuck(self, tmp_config, tmp_dir):
        tmp_config.max_iterations = 2
        orch = self._make_orch(tmp_config, tmp_dir)

        always_changes = AgentResult(
            success=True,
            raw_output=MOCK_REVIEW_CHANGES,
            parsed=json.loads(MOCK_REVIEW_CHANGES),
        )
        orch.claude.call = MagicMock(side_effect=[
            AgentResult(success=True, raw_output=MOCK_ARCHITECT_RESPONSE,
                        parsed=json.loads(MOCK_ARCHITECT_RESPONSE)),
            always_changes,
            always_changes,
            always_changes,
        ])
        orch.gemini.call = MagicMock(return_value=AgentResult(success=True, raw_output="Done"))

        task = orch.create_task("Impossible task")
        task = orch.run(task.task_id)

        assert task.status == TaskStatus.STUCK

    def test_claude_failure_sets_failed(self, tmp_config, tmp_dir):
        orch = self._make_orch(tmp_config, tmp_dir)
        orch.claude.call = MagicMock(return_value=AgentResult(
            success=False, error="API error"
        ))
        task = orch.create_task("Task that will fail")
        task = orch.run(task.task_id)
        assert task.status == TaskStatus.FAILED

    def test_artifacts_written(self, tmp_config, tmp_dir):
        orch = self._make_orch(tmp_config, tmp_dir)
        orch.claude.call = MagicMock(side_effect=[
            AgentResult(success=True, raw_output=MOCK_ARCHITECT_RESPONSE,
                        parsed=json.loads(MOCK_ARCHITECT_RESPONSE)),
            AgentResult(success=True, raw_output=MOCK_REVIEW_APPROVED,
                        parsed=json.loads(MOCK_REVIEW_APPROVED)),
        ])
        orch.gemini.call = MagicMock(return_value=AgentResult(success=True, raw_output="Done"))

        task = orch.create_task("Check artifacts")
        task = orch.run(task.task_id)

        run_dir = tmp_config.runs_dir / task.task_id
        assert (run_dir / "state.json").exists()
        assert (run_dir / "architect_plan.json").exists()

        state = json.loads((run_dir / "state.json").read_text())
        assert state["status"] == "APPROVED"
