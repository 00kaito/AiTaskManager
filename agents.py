"""
agents.py — wrappery subprocess dla Claude Code CLI i Gemini CLI

Każde wywołanie:
  - ma timeout
  - ma retry z backoff
  - waliduje output
  - zwraca ustrukturyzowany wynik
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Wynik wywołania agenta
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
        """Zwraca sparsowany JSON lub rzuca ValueError."""
        if self.parsed is not None:
            return self.parsed
        raise ValueError(f"No parsed JSON. Raw output: {self.raw_output[:500]}")


# ─────────────────────────────────────────────
# Bazowa klasa agenta
# ─────────────────────────────────────────────

class BaseAgent:
    name: str = "agent"

    def _run_subprocess(
        self,
        cmd: list[str],
        cwd: Optional[Path],
        timeout: int,
    ) -> tuple[str, str, int]:
        """Uruchamia subprocess i zwraca (stdout, stderr, returncode)."""
        logger.debug(f"[{self.name}] CMD: {' '.join(cmd[:4])}...")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(cwd) if cwd else None,
            )
            return proc.stdout.strip(), proc.stderr.strip(), proc.returncode
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"[{self.name}] Timeout after {timeout}s")

    def _parse_json(self, raw: str) -> Optional[dict | list]:
        """Próbuje sparsować JSON — odporna na markdown fences."""
        text = raw.strip()

        # Usuń markdown fences jeśli agent je doda mimo instrukcji
        if text.startswith("```"):
            lines = text.split("\n")
            # Usuń pierwszą i ostatnią linię jeśli to fences
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        # Znajdź pierwszy { lub [ i ostatni } lub ]
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
                stdout, stderr, code = self._run_subprocess(cmd, cwd, timeout)
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
        Wywołuje `claude -p <prompt> --output-format json`.
        expect_json=True → waliduje i parsuje JSON w odpowiedzi.
        """
        effective_timeout = timeout or config.claude_timeout

        cmd = [
            config.claude_bin,
            "--print",           # nie-interaktywny tryb
            "--output-format", "json",
            prompt,
        ]

        logger.info(
            f"[claude] Calling Claude Code "
            f"(timeout={effective_timeout}s, json={expect_json})"
        )
        return self._call_with_retry(cmd, cwd, effective_timeout, expect_json)

    def call_with_file_context(
        self,
        prompt: str,
        context_files: list[Path],
        cwd: Optional[Path] = None,
        expect_json: bool = True,
    ) -> AgentResult:
        """Dodaje pliki jako context przez --context flag (jeśli Claude Code wspiera)."""
        # Jeśli Claude Code nie ma --context, wpleć pliki do prompta
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
        timeout: Optional[int] = None,
    ) -> AgentResult:
        """
        Wywołuje Gemini CLI z podanym promptem.
        Gemini CLI nie zwraca JSON — traktujemy output jako tekst.
        """
        effective_timeout = timeout or config.gemini_timeout

        # Gemini CLI — dostosuj flagę jeśli twój CLI ma inną składnię
        cmd = [
            config.gemini_bin,
            "--prompt", prompt,
            "--yolo",          # auto-accept file changes (dostosuj do swojego CLI)
        ]

        logger.info(
            f"[gemini] Calling Gemini CLI "
            f"(timeout={effective_timeout}s, cwd={cwd})"
        )
        # Gemini pisze do plików, nie zwraca JSON
        return self._call_with_retry(cmd, cwd, effective_timeout, expect_json=False)


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
        """Zwraca (opis zmienionych plików, suma linii changed)."""
        stat, code = self._git("diff", "--stat", "HEAD")
        if code != 0:
            stat, code = self._git("diff", "--stat")  # fallback: unstaged
        if not stat:
            return "no changes", 0

        # Parsuj ostatnią linię: "3 files changed, 45 insertions(+), 12 deletions(-)"
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
        """Zwraca SHA aktualnego HEAD."""
        sha, code = self._git("rev-parse", "HEAD")
        return sha if code == 0 else ""

    def full_diff_from_sha(self, start_sha: str, max_chars: int = 15000) -> str:
        """Pełny diff od podanego SHA (czyli od startu taska) do HEAD."""
        if not start_sha:
            return self.full_diff(max_chars)
        diff, code = self._git("diff", start_sha, "HEAD")
        if code != 0 or not diff:
            diff, _ = self._git("diff", start_sha)
        return diff[:max_chars] if diff else "(no diff from task start)"

    def full_diff(self, max_chars: int = 10000) -> str:
        """Pełny diff od ostatniego commita."""
        diff, _ = self._git("diff", "HEAD")
        if not diff:
            diff, _ = self._git("diff")
        return diff[:max_chars] if diff else "(no diff available)"

    def stage_and_commit(self, message: str) -> bool:
        """Auto-commit po każdej iteracji — dla audytu."""
        self._git("add", "-A")
        _, code = self._git("commit", "-m", message)
        return code == 0
