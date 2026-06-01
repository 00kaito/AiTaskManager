"""
agents.py — subprocess wrappers for Claude Code CLI and Gemini CLI

Each call:
  - has a timeout
  - has a retry with backoff
  - validates the output
  - returns a structured result
"""

import io
import json
import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Agent call result
# ─────────────────────────────────────────────

@dataclass
class AgentResult:
    success: bool
    raw_output: str = ""
    parsed: Optional[dict | list] = None
    error: str = ""
    attempts: int = 1
    duration_sec: float = 0.0

    def json(self) -> dict | list:
        """Returns parsed JSON or raises ValueError."""
        if self.parsed is not None:
            return self.parsed
        raise ValueError(f"No parsed JSON. Raw output: {self.raw_output[:500]}")


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str  # "anthropic" | "google" | "ollama" | "litellm"
    model_id: str
    base_url: Optional[str] = None
    extra_args: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
# Base agent class
# ─────────────────────────────────────────────

class AgentRuntime:
    """
    Template-method base. A concrete runtime supplies:
      - build_command(model, instruction) -> list[str]   (required)
      - post_process(raw, expect_json) -> AgentResult    (optional; default: return raw)

    Shared logic (subprocess streaming, retry, JSON parsing, prompt-file
    handling) lives here. New runtimes should not duplicate any of it.
    """
    name: str = "runtime"
    default_timeout: int = 300
    prompt_prefix: str = "agent"

    def build_command(self, model: ModelEndpoint, instruction: str) -> list[str]:
        raise NotImplementedError

    def post_process(self, raw: AgentResult, expect_json: bool) -> AgentResult:
        return raw

    # Override to True when stdout is wrapped in a runtime envelope (e.g. Claude
    # Code emits JSON envelope around the model's reply). The base retry loop
    # then validates the envelope, not the model's payload, on every attempt.
    retry_validates_stdout_json: bool = False

    def invoke(
        self,
        prompt: str,
        model: ModelEndpoint,
        cwd: Optional[Path] = None,
        expect_json: bool = True,
        timeout: Optional[int] = None,
    ) -> AgentResult:
        effective_timeout = timeout or self.default_timeout
        prompt_file, instruction = self._prepare_prompt_file(prompt)
        try:
            cmd = self.build_command(model, instruction)
            logger.info(
                f"[{self.name}] invoke (model={model.model_id}, "
                f"timeout={effective_timeout}s, json={expect_json}, cwd={cwd})"
            )
            retry_expect_json = self.retry_validates_stdout_json or expect_json
            raw = self._call_with_retry(cmd, cwd, effective_timeout, expect_json=retry_expect_json)
            if not raw.success:
                return raw
            return self.post_process(raw, expect_json=expect_json)
        finally:
            self._cleanup_prompt_file(prompt_file)

    def _prepare_prompt_file(self, prompt: str) -> tuple[Path, str]:
        temp_dir = config.base_dir / ".orchestrator"
        temp_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = temp_dir / f"prompt_{self.prompt_prefix}_{int(time.time())}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        instruction = f"Read and execute instructions from file: {prompt_file}"
        return prompt_file, instruction

    @staticmethod
    def _cleanup_prompt_file(prompt_file: Path) -> None:
        try:
            prompt_file.unlink()
        except Exception:
            pass

    def _run_subprocess_streaming(
        self,
        cmd: list[str],
        cwd: Optional[Path],
        timeout: int,
    ) -> tuple[str, str, int]:
        """Runs a subprocess with real-time log streaming."""
        logger.debug(f"[{self.name}] CMD: {' '.join(cmd[:4])}...")
        
        stdout_lines = []
        stderr_lines = []
        
        # Windows needs shell=True for some aliases/batch files, 
        # but list commands are safer without it unless needed.
        use_shell = os.name == 'nt'
        
        # Use Popen instead of run to be able to read streams on the fly
        proc = subprocess.Popen(
            cmd if not use_shell else subprocess.list2cmdline(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            bufsize=1,  # line buffered
            universal_newlines=True,
            shell=use_shell
        )

        def read_stream(stream: io.TextIOBase, target_list: list, is_stderr: bool):
            for line in stream:
                line_stripped = line.rstrip()
                target_list.append(line)
                # Log each line in real-time
                log_prefix = f"[{self.name}][ERR]" if is_stderr else f"[{self.name}]"
                logger.info(f"{log_prefix} {line_stripped}")

        # Threads to read stdout and stderr in parallel
        t1 = threading.Thread(target=read_stream, args=(proc.stdout, stdout_lines, False))
        t2 = threading.Thread(target=read_stream, args=(proc.stderr, stderr_lines, True))
        t1.start()
        t2.start()

        try:
            # Wait for process completion with timeout
            return_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            t1.join()
            t2.join()
            raise TimeoutError(f"[{self.name}] Timeout after {timeout}s")

        t1.join()
        t2.join()
        
        return "".join(stdout_lines), "".join(stderr_lines), return_code

    def _parse_json(self, raw: str) -> Optional[dict | list]:
        """Tries to parse JSON — resistant to markdown fences."""
        text = raw.strip()

        # Remove markdown fences if the agent adds them despite instructions
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines if they are fences
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Find first { or [ and last } or ]
        start = -1
        for i, ch in enumerate(text):
            if ch in "{[":
                start = i
                break
        if start == -1:
            return None

        end = -1
        for i in range(len(text) - 1, -1, -1):
            if text[i] in "}]":
                end = i
                break
        if end == -1:
            return None

        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            logger.warning(f"[{self.name}] JSON parse failed: {e}")
            return None

    def _call_with_retry(
        self,
        cmd: list[str],
        cwd: Optional[Path],
        timeout: int,
        expect_json: bool,
    ) -> AgentResult:
        last_error = ""
        for attempt in range(1, config.agent_max_retries + 1):
            start = time.monotonic()
            try:
                stdout, stderr, code = self._run_subprocess_streaming(cmd, cwd, timeout)
                duration = time.monotonic() - start

                if code != 0:
                    last_error = f"exit code {code}: {stderr[:300]}"
                    logger.warning(
                        f"[{self.name}] attempt {attempt} failed: {last_error}"
                    )
                else:
                    parsed = self._parse_json(stdout) if expect_json else None
                    if expect_json and parsed is None:
                        last_error = f"Expected JSON but got: {stdout[:200]}"
                        logger.warning(
                            f"[{self.name}] attempt {attempt} — {last_error}"
                        )
                    else:
                        logger.info(
                            f"[{self.name}] success on attempt {attempt} "
                            f"({duration:.1f}s)"
                        )
                        return AgentResult(
                            success=True,
                            raw_output=stdout,
                            parsed=parsed,
                            attempts=attempt,
                            duration_sec=duration,
                        )

            except TimeoutError as e:
                last_error = str(e)
                logger.warning(f"[{self.name}] attempt {attempt} timed out")

            if attempt < config.agent_max_retries:
                delay = config.agent_retry_delay * attempt
                logger.info(f"[{self.name}] retrying in {delay:.0f}s...")
                time.sleep(delay)

        return AgentResult(
            success=False,
            error=last_error,
            attempts=config.agent_max_retries,
        )


class BaseAgent:
    def __init__(self, runtime: AgentRuntime, model: ModelEndpoint):
        self.runtime = runtime
        self.model = model

    @property
    def name(self) -> str:
        return f"{self.runtime.name}/{self.model.model_id}"

    def call(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        expect_json: bool = True,
        timeout: Optional[int] = None,
    ) -> AgentResult:
        return self.runtime.invoke(
            prompt=prompt,
            model=self.model,
            cwd=cwd,
            expect_json=expect_json,
            timeout=timeout
        )


# ─────────────────────────────────────────────
# Claude Code CLI
# ─────────────────────────────────────────────

class ClaudeCodeRuntime(AgentRuntime):
    name = "claude"
    prompt_prefix = "claude"
    retry_validates_stdout_json = True  # envelope is always JSON

    @property
    def default_timeout(self) -> int:
        return config.claude_timeout

    def build_command(self, model: ModelEndpoint, instruction: str) -> list[str]:
        # Claude wraps stdout in a JSON envelope; ask it for JSON-only payload.
        instruction = f"{instruction}. Respond with ONLY the required JSON."
        return [
            config.claude_bin,
            "--model", model.model_id,
            "--print",
            "--output-format", "json",
            instruction,
        ]

    def post_process(self, raw: AgentResult, expect_json: bool) -> AgentResult:
        envelope = self._parse_json(raw.raw_output)
        if not isinstance(envelope, dict):
            return AgentResult(
                success=False,
                error=f"Unparsable CLI response: {raw.raw_output[:200]}",
                raw_output=raw.raw_output,
                attempts=raw.attempts,
                duration_sec=raw.duration_sec,
            )

        if envelope.get("is_error"):
            return AgentResult(
                success=False,
                error=envelope.get("result", "Claude returned an error"),
                raw_output=raw.raw_output,
                attempts=raw.attempts,
                duration_sec=raw.duration_sec,
            )

        claude_text = envelope.get("result", "")

        if not expect_json:
            return AgentResult(
                success=True,
                raw_output=claude_text,
                attempts=raw.attempts,
                duration_sec=raw.duration_sec,
            )

        parsed = self._parse_json(claude_text)
        if parsed is None:
            logger.warning(f"[{self.name}] No JSON in response: {claude_text[:200]}")
            return AgentResult(
                success=False,
                error=f"Claude did not return JSON: {claude_text[:200]}",
                raw_output=claude_text,
                attempts=raw.attempts,
                duration_sec=raw.duration_sec,
            )

        return AgentResult(
            success=True,
            raw_output=claude_text,
            parsed=parsed,
            attempts=raw.attempts,
            duration_sec=raw.duration_sec,
        )


# ─────────────────────────────────────────────
# Gemini CLI
# ─────────────────────────────────────────────

class GeminiCliRuntime(AgentRuntime):
    name = "gemini"
    prompt_prefix = "gemini"

    @property
    def default_timeout(self) -> int:
        return config.gemini_timeout

    def build_command(self, model: ModelEndpoint, instruction: str) -> list[str]:
        return [
            config.gemini_bin,
            "--model", model.model_id,
            "--prompt", instruction,
            "--yolo",
        ]

    def post_process(self, raw: AgentResult, expect_json: bool) -> AgentResult:
        # Gemini emits the model output directly to stdout (no envelope).
        # When the caller expects JSON, parse it now.
        if not expect_json:
            return raw
        parsed = self._parse_json(raw.raw_output)
        if parsed is None:
            return AgentResult(
                success=False,
                error=f"Expected JSON but got: {raw.raw_output[:200]}",
                raw_output=raw.raw_output,
                attempts=raw.attempts,
                duration_sec=raw.duration_sec,
            )
        return AgentResult(
            success=True,
            raw_output=raw.raw_output,
            parsed=parsed,
            attempts=raw.attempts,
            duration_sec=raw.duration_sec,
        )


# ─────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────

RUNTIMES = {
    "claude": ClaudeCodeRuntime,
    "gemini": GeminiCliRuntime,
    # "aider": AiderRuntime,
}

def _resolve_model_endpoint(runtime: str, model: ModelEndpoint | str | None) -> ModelEndpoint:
    if isinstance(model, ModelEndpoint):
        return model
    
    # Defaults from config
    if runtime == "claude":
        model_id = model if isinstance(model, str) else config.claude_model
        return ModelEndpoint(provider="anthropic", model_id=model_id)
    elif runtime == "gemini":
        model_id = model if isinstance(model, str) else config.gemini_model
        return ModelEndpoint(provider="google", model_id=model_id)
    
    # Generic fallback
    model_id = model if isinstance(model, str) else "unknown"
    return ModelEndpoint(provider="unknown", model_id=model_id)

def create_agent(runtime: str, model: ModelEndpoint | str | None = None) -> BaseAgent:
    """
    Creates an agent with a specific runtime and model.
    runtime: 'claude' | 'gemini' | ...
    model: ModelEndpoint, model_id string, or None (uses config default).
    """
    runtime_name = runtime.strip().lower()
    if runtime_name not in RUNTIMES:
        raise ValueError(f"Unknown runtime: {runtime_name!r}. Available: {list(RUNTIMES.keys())}")
    
    runtime_cls = RUNTIMES[runtime_name]
    endpoint = _resolve_model_endpoint(runtime_name, model)
    return BaseAgent(runtime=runtime_cls(), model=endpoint)

# ─────────────────────────────────────────────
# Hook for Aider (next task)
# ─────────────────────────────────────────────

# class AiderRuntime(AgentRuntime):
#     """
#     TODO: implement in the next task.
#
#     Only build_command (and optionally post_process) need to be supplied —
#     the base class handles prompt-file lifecycle, retry, streaming, JSON parsing.
#
#     Notes for the implementer:
#       - Aider expects litellm-style ids: f"{model.provider}/{model.model_id}"
#         e.g. "ollama/qwen3-coder:32b" or "anthropic/claude-sonnet-4-5".
#       - Aider edits files directly on disk and commits via git; stdout carries
#         a status report. If we need structured output, post_process can parse it.
#     """
#     name = "aider"
#     prompt_prefix = "aider"
#
#     def build_command(self, model: ModelEndpoint, instruction: str) -> list[str]:
#         model_ref = f"{model.provider}/{model.model_id}"
#         return [config.aider_bin, "--model", model_ref, "--message", instruction, "--yes"]


# ─────────────────────────────────────────────
# Git helpers
# ─────────────────────────────────────────────

class GitHelper:
    def __init__(self, repo_root: Path):
        self.root = repo_root

    def _git(self, *args: str) -> tuple[str, int]:
        try:
            proc = subprocess.run(
                [config.git_bin, *args],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.root),
            )
            return proc.stdout.strip(), proc.returncode
        except Exception as e:
            logger.warning(f"[git] error: {e}")
            return "", 1

    def diff_stat(self) -> tuple[str, int]:
        """Returns (diff stat description, total lines changed)."""
        stat, code = self._git("diff", "--stat", "HEAD")
        if code != 0:
            stat, code = self._git("diff", "--stat")  # fallback: unstaged
        if not stat:
            return "no changes", 0

        # Parse last line: "3 files changed, 45 insertions(+), 12 deletions(-)"
        total_lines = 0
        last_line = stat.strip().split("\n")[-1]
        for token in last_line.split(","):
            token = token.strip()
            if "insertion" in token or "deletion" in token:
                try:
                    total_lines += int(token.split()[0])
                except ValueError:
                    pass

        return stat, total_lines

    def get_current_sha(self) -> str:
        """Returns the SHA of current HEAD."""
        sha, code = self._git("rev-parse", "HEAD")
        return sha if code == 0 else ""

    def full_diff_from_sha(self, start_sha: str, max_chars: int = 15000) -> str:
        """Full diff from given SHA (task start) to HEAD."""
        if not start_sha:
            return self.full_diff(max_chars)
        diff, code = self._git("diff", start_sha, "HEAD")
        if code != 0 or not diff:
            diff, _ = self._git("diff", start_sha)
        return diff[:max_chars] if diff else "(no diff from task start)"

    def full_diff(self, max_chars: int = 10000) -> str:
        """Full diff from last commit."""
        diff, _ = self._git("diff", "HEAD")
        if not diff:
            diff, _ = self._git("diff")
        return diff[:max_chars] if diff else "(no diff available)"

    def stage_and_commit(self, message: str) -> bool:
        """Auto-commit after each iteration — for audit."""
        self._git("add", "-A")
        _, code = self._git("commit", "-m", message)
        return code == 0

    def squash_commits(self, start_sha: str, message: str) -> bool:
        """Squashes all commits from start_sha into one with a new message."""
        if not start_sha:
            return False
        # 1. Reset HEAD to start_sha, leaving changes in index
        _, code = self._git("reset", "--soft", start_sha)
        if code != 0:
            return False
        # 2. Make one clean commit with description from Reviewer
        _, code = self._git("commit", "-m", message)
        return code == 0
