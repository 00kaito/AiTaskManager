"""
config.py — central orchestrator configuration
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _find_bin(name: str, extra_paths: list[str] = None) -> str:
    """Searches for a binary in PATH, then in known locations (nvm, local bins)."""
    found = shutil.which(name)
    if found:
        return found
    candidates = extra_paths or []
    # Common nvm / npm global locations for all users in /home
    home_dirs = list(Path("/home").iterdir()) if Path("/home").exists() else []
    for home in home_dirs:
        nvm_base = home / ".nvm" / "versions" / "node"
        if nvm_base.exists():
            for node_ver in sorted(nvm_base.iterdir(), reverse=True):
                candidates.append(str(node_ver / "bin" / name))
        candidates.append(str(home / ".local" / "bin" / name))
    candidates += [f"/usr/local/bin/{name}", f"/usr/bin/{name}"]
    for path in candidates:
        if Path(path).is_file():
            return path
    return name  # fallback — return original name, subprocess will raise a readable error


def _find_project_root() -> Path:
    """Detects the project root (git top-level) or returns CWD."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return Path.cwd()


@dataclass
class OrchestratorConfig:
    # --- Paths (automatically detects project root) ---
    base_dir: Path = field(default_factory=_find_project_root)
    runs_dir: Path = field(default=None)
    db_path: Path = field(default=None)

    # --- Loop limits ---
    max_iterations: int = 6          # max IMPLEMENTING → REVIEWING rounds
    max_stuck_rounds: int = 2        # how many rounds without diff before STUCK
    min_diff_lines: int = 1          # minimal lines changed not to be "stuck"

    # --- Timeouts (seconds) ---
    claude_timeout: int = 300        # 5 min for architecture / review
    gemini_timeout: int = 1200        # 10 min for implementation

    # --- Retry ---
    agent_max_retries: int = 3
    agent_retry_delay: float = 5.0   # seconds between retries

    # --- Claude Code CLI ---
    claude_bin: str = field(default_factory=lambda: _find_bin("claude"))
    claude_model: str = "claude-opus-4-5"   # optionally, if you want to force a model

    # --- Gemini CLI ---
    gemini_bin: str = field(default_factory=lambda: _find_bin("gemini"))
    gemini_model: str = "gemini-2.5-pro"

    # --- Role assignment: runtime ('claude', 'gemini') and model_id ---
    architect_runtime: str = "gemini"
    architect_model: Optional[str] = None
    
    analyzer_runtime: str = "gemini"
    analyzer_model: Optional[str] = None
    
    developer_runtime: str = "gemini"
    developer_model: Optional[str] = None
    
    reviewer_runtime: str = "gemini"
    reviewer_model: Optional[str] = None

    # --- Backwards compatibility aliases ---
    @property
    def architect_role(self) -> str: return self.architect_runtime
    @architect_role.setter
    def architect_role(self, v: str): self.architect_runtime = v

    @property
    def analyzer_role(self) -> str: return self.analyzer_runtime
    @analyzer_role.setter
    def analyzer_role(self, v: str): self.analyzer_runtime = v

    @property
    def developer_role(self) -> str: return self.developer_runtime
    @developer_role.setter
    def developer_role(self, v: str): self.developer_runtime = v

    @property
    def reviewer_role(self) -> str: return self.reviewer_runtime
    @reviewer_role.setter
    def reviewer_role(self, v: str): self.reviewer_runtime = v

    # --- Phase options ---
    use_analyzer: bool = False       # whether to run an additional code analysis phase


    # --- Git ---
    use_git: bool = True             # whether to perform git diff between iterations
    git_bin: str = "git"

    # --- Logging ---
    log_level: str = "INFO"          # DEBUG | INFO | WARNING | ERROR
    log_to_file: bool = True

    def __post_init__(self):
        """Initializes paths dependent on base_dir."""
        if self.runs_dir is None:
            self.runs_dir = self.base_dir / ".orchestrator" / "runs"
        if self.db_path is None:
            self.db_path = self.base_dir / ".orchestrator" / "orchestrator.db"


# Singleton – import this instance everywhere
config = OrchestratorConfig()


def override_from_env() -> None:
    """Override config from environment variables (optional)."""
    if v := os.getenv("ORCH_MAX_ITERATIONS"):
        config.max_iterations = int(v)
    if v := os.getenv("ORCH_CLAUDE_TIMEOUT"):
        config.claude_timeout = int(v)
    if v := os.getenv("ORCH_GEMINI_TIMEOUT"):
        config.gemini_timeout = int(v)
    if v := os.getenv("ORCH_RUNS_DIR"):
        config.runs_dir = Path(v)
    if v := os.getenv("ORCH_DB_PATH"):
        config.db_path = Path(v)
    if v := os.getenv("ORCH_USE_GIT"):
        config.use_git = v.lower() in ("1", "true", "yes")
    if v := os.getenv("ORCH_GEMINI_BIN"):
        config.gemini_bin = v
    if v := os.getenv("ORCH_CLAUDE_BIN"):
        config.claude_bin = v
    # New runtime/model env vars
    if v := os.getenv("ORCH_ARCHITECT_RUNTIME"): config.architect_runtime = v
    if v := os.getenv("ORCH_ARCHITECT_MODEL"): config.architect_model = v
    if v := os.getenv("ORCH_ANALYZER_RUNTIME"): config.analyzer_runtime = v
    if v := os.getenv("ORCH_ANALYZER_MODEL"): config.analyzer_model = v
    if v := os.getenv("ORCH_DEVELOPER_RUNTIME"): config.developer_runtime = v
    if v := os.getenv("ORCH_DEVELOPER_MODEL"): config.developer_model = v
    if v := os.getenv("ORCH_REVIEWER_RUNTIME"): config.reviewer_runtime = v
    if v := os.getenv("ORCH_REVIEWER_MODEL"): config.reviewer_model = v

    # Backwards compatibility env vars
    if v := os.getenv("ORCH_ARCHITECT_ROLE"): config.architect_runtime = v
    if v := os.getenv("ORCH_ANALYZER_ROLE"): config.analyzer_runtime = v
    if v := os.getenv("ORCH_DEVELOPER_ROLE"): config.developer_runtime = v
    if v := os.getenv("ORCH_REVIEWER_ROLE"): config.reviewer_runtime = v

