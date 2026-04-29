"""
config.py — centralna konfiguracja orkiestratora
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def _find_bin(name: str, extra_paths: list[str] = None) -> str:
    """Szuka binarki w PATH, a potem w znanych lokalizacjach (nvm, local bins)."""
    found = shutil.which(name)
    if found:
        return found
    candidates = extra_paths or []
    # Wspólne lokalizacje nvm / npm global dla wszystkich userów w /home
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
    return name  # fallback — zwróci oryginalną nazwę, subprocess rzuci czytelny błąd


def _find_project_root() -> Path:
    """Wykrywa root projektu (git top-level) lub zwraca CWD."""
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
    # --- Ścieżki (automatycznie wykrywa root projektu) ---
    base_dir: Path = field(default_factory=_find_project_root)
    runs_dir: Path = field(default=None)
    db_path: Path = field(default=None)

    # --- Limity pętli ---
    max_iterations: int = 6          # max rund IMPLEMENTING → REVIEWING
    max_stuck_rounds: int = 2        # ile rund bez diff zanim STUCK
    min_diff_lines: int = 1          # minimalna zmiana linii żeby nie być "stuck"

    # --- Timeouty (sekundy) ---
    claude_timeout: int = 300        # 5 min na architekturę / review
    gemini_timeout: int = 600        # 10 min na implementację

    # --- Retry ---
    agent_max_retries: int = 3
    agent_retry_delay: float = 5.0   # sekundy między retry

    # --- Claude Code CLI ---
    claude_bin: str = field(default_factory=lambda: _find_bin("claude"))
    claude_model: str = "claude-opus-4-5"   # opcjonalnie, jeśli chcesz wymusić model

    # --- Gemini CLI ---
    gemini_bin: str = field(default_factory=lambda: _find_bin("gemini"))
    gemini_model: str = "gemini-2.5-pro"

    # --- Role assignment: "claude" or "gemini" ---
    architect_role: str = "claude"
    developer_role: str = "gemini"
    reviewer_role: str = "gemini"

    # --- Git ---
    use_git: bool = True             # czy robić git diff między iteracjami
    git_bin: str = "git"

    # --- Logowanie ---
    log_level: str = "INFO"          # DEBUG | INFO | WARNING | ERROR
    log_to_file: bool = True

    def __post_init__(self):
        """Inicjalizuje ścieżki zależne od base_dir."""
        if self.runs_dir is None:
            self.runs_dir = self.base_dir / ".orchestrator" / "runs"
        if self.db_path is None:
            self.db_path = self.base_dir / ".orchestrator" / "orchestrator.db"


# Singleton – importuj tę instancję wszędzie
config = OrchestratorConfig()


def override_from_env() -> None:
    """Nadpisz config ze zmiennych środowiskowych (opcjonalne)."""
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
    if v := os.getenv("ORCH_ARCHITECT_ROLE"):
        config.architect_role = v
    if v := os.getenv("ORCH_DEVELOPER_ROLE"):
        config.developer_role = v
    if v := os.getenv("ORCH_REVIEWER_ROLE"):
        config.reviewer_role = v
