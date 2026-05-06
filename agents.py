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
import subprocess
import threading
import time
from dataclasses import dataclass
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


# ─────────────────────────────────────────────
# Base agent class
# ─────────────────────────────────────────────

class BaseAgent:
    name: str = "agent"

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
        
        # Use Popen instead of run to be able to read streams on the fly
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            bufsize=1,  # line buffered
            universal_newlines=True
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


# ─────────────────────────────────────────────
# Claude Code CLI
# ─────────────────────────────────────────────

class ClaudeAgent(BaseAgent):
    name = "claude"

    def call(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        expect_json: bool = True,
        timeout: Optional[int] = None,
    ) -> AgentResult:
        """
        Calls `claude --print --output-format json`.
        Uses a temporary file for the prompt to avoid CLI argument limits.
        """
        effective_timeout = timeout or config.claude_timeout

        # Save prompt to temporary file
        temp_dir = config.base_dir / ".orchestrator"
        temp_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = temp_dir / f"prompt_claude_{int(time.time())}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        # Instruction for Claude to read the file and execute the task
        instruction = f"Read and execute instructions from file: {prompt_file}. Respond with ONLY the required JSON."

        cmd = [
            config.claude_bin,
            "--print",
            "--output-format", "json",
            instruction,
        ]

        logger.info(
            f"[claude] Calling Claude Code via prompt file "
            f"(timeout={effective_timeout}s, json={expect_json})"
        )

        raw = self._call_with_retry(cmd, cwd, effective_timeout, expect_json=False)
        
        # Remove file after use
        try: prompt_file.unlink()
        except: pass

        if not raw.success:
            return raw

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
            logger.warning(f"[claude] No JSON in response: {claude_text[:200]}")
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

    def call_with_file_context(
        self,
        prompt: str,
        context_files: list[Path],
        cwd: Optional[Path] = None,
        expect_json: bool = True,
    ) -> AgentResult:
        """Adds files as context via --context flag (if Claude Code supports it)."""
        # If Claude Code doesn't have --context, embed files into the prompt
        file_contents = []
        for f in context_files:
            if f.exists():
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    file_contents.append(f"### File: {f}\n```\n{content[:3000]}\n```")
                except Exception:
                    pass

        full_prompt = prompt
        if file_contents:
            full_prompt = "\n\n".join(file_contents) + "\n\n" + prompt

        return self.call(full_prompt, cwd, expect_json)


# ─────────────────────────────────────────────
# Gemini CLI
# ─────────────────────────────────────────────

class GeminiAgent(BaseAgent):
    name = "gemini"

    def call(
        self,
        prompt: str,
        cwd: Optional[Path] = None,
        expect_json: bool = False,
        timeout: Optional[int] = None,
    ) -> AgentResult:
        """Calls Gemini CLI via temporary prompt file."""
        effective_timeout = timeout or config.gemini_timeout

        # Save prompt to temporary file
        temp_dir = config.base_dir / ".orchestrator"
        temp_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = temp_dir / f"prompt_gemini_{int(time.time())}.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        instruction = f"Read and execute instructions from file: {prompt_file}"

        cmd = [
            config.gemini_bin,
            "--prompt", instruction,
            "--yolo",
        ]

        logger.info(
            f"[gemini] Calling Gemini CLI via prompt file "
            f"(timeout={effective_timeout}s, json={expect_json}, cwd={cwd})"
        )
        res = self._call_with_retry(cmd, cwd, effective_timeout, expect_json=expect_json)
        
        # Remove file after use
        try: prompt_file.unlink()
        except: pass

        return res


# ─────────────────────────────────────────────
# Agent factory
# ─────────────────────────────────────────────

def create_agent(model: str) -> BaseAgent:
    """Return a ClaudeAgent or GeminiAgent based on model name ('claude' or 'gemini')."""
    model = model.strip().lower()
    if model == "claude":
        return ClaudeAgent()
    if model == "gemini":
        return GeminiAgent()
    raise ValueError(f"Unknown model: {model!r}. Use 'claude' or 'gemini'")


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
