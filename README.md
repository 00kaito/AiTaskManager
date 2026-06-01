# AI Task Orchestrator (v2.1)

A stateful task orchestrator working with **Claude Code** and **Gemini CLI**.  
Automates the full development lifecycle: **Architecture → Implementation → Review → Iteration → Success**.

The tool is **completely project-independent** — install it once and use it in any repository or directory.

---

## Description

The AI Task Orchestrator is a robust multi-agent system designed to automate complex software engineering tasks. It uses a state-machine-driven approach to coordinate different AI models and tools, ensuring that tasks are not just implemented, but also planned and verified against specific acceptance criteria.

### Multi-Agent Orchestration
The orchestrator manages three specialized AI agent roles:

1.  **Architect**: Analyzes the codebase, creates a detailed implementation plan in JSON format, and defines verifiable acceptance criteria.
2.  **Developer**: Receives the plan and modifies project files. The developer has permissions to edit code, create new files, and delete old ones. After each iteration, it generates an `implementation_report.md`.
3.  **Reviewer**: Checks the diff of changes against the acceptance criteria. If everything is ready — approves (`APPROVED`). If not — returns the task for fixes (`CHANGES_REQUESTED`) with specific feedback.

---

## Architectural Overview

The project follows a state-machine architecture that guides a task through several logical phases:

1.  **Architecting**: The Architect agent analyzes the requirements and the codebase to produce a structured JSON plan and acceptance criteria.
2.  **Implementing**: The Developer agent executes the plan, performing file-system operations and running commands.
3.  **Reviewing**: The Reviewer agent compares the resulting state with the acceptance criteria.
4.  **Awaiting Human / Human Feedback**: (Optional) If `--human-review` is enabled, the process pauses for manual verification and feedback, which is then fed back into the loop.

### 🧩 Modern Architecture: Runtime vs Model Split

The orchestrator decouples **how** an agent interacts with your system from **which** AI model powers it.

*   **Agent Runtime**: The "body" of the agent. It defines the tool used to execute tasks (e.g., `ClaudeCodeRuntime`, `GeminiCliRuntime`). Runtimes handle file operations, command execution, and response parsing.
*   **Model Endpoint**: The "brain" of the agent. It specifies the LLM version (e.g., `claude-3-7-sonnet-latest`, `gemini-2.0-pro-exp-02-05`).

---

## Core Components

### `Orchestrator` (`runner.py`)
The central engine of the application. It manages the task lifecycle, handles state transitions, coordinates agents, and manages the execution loop. It is responsible for logging conversations and writing task artifacts.

### `BaseAgent` (`agents.py`)
An abstraction layer for AI agents. It encapsulates the runtime and model, providing a consistent interface (`call` method) for the orchestrator to interact with different AI backends like Claude Code or Gemini CLI.

### `TaskRepository` (`state.py`)
Handles persistence of tasks and their history using an SQLite database. It provides methods for saving, loading, and listing tasks, ensuring that the state of the orchestration is preserved across sessions.

---

## Installation (Professional and Safe)

The recommended method for installing CLI tools is to isolate them in a separate virtual environment to avoid conflicts with system packages (the `externally-managed-environment` error).

### Method 1: Automatic (via pipx)
The safest standard for CLI tools. Installs the application in an isolated environment and automatically creates symlinks.
```bash
pipx install .
```
*(If you don't have pipx: `sudo apt install pipx && pipx ensurepath`)*

### Method 2: Manual (Venv + Symlink)
If you prefer full control without additional tools:

1. **Create an isolated environment** inside the orchestrator directory:
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install .
   ```

2. **Create symbolic links** in your local binary folder:
   ```bash
   mkdir -p ~/.local/bin
   ln -s $(pwd)/venv/bin/orch ~/.local/bin/orch
   ln -s $(pwd)/venv/bin/orch-monitor ~/.local/bin/orch-monitor
   ```

After following one of the above methods, the `orch` and `orch-monitor` commands will be available globally without risking system stability.

---

## Quick Start

1.  **Enter your project directory** (ideally a git repository).
2.  **Initialize a task**:
    ```bash
    orch new "Refactor the parser in src/parser.py to a functional approach and add tests"
    ```
    The orchestrator will automatically detect the project root (via Git or CWD) and create a `.orchestrator/` directory for data. 
    *Hint: Add `.orchestrator/` to your `.gitignore`.*

3.  **Run the process**:
    ```bash
    orch run TASK-XXXXXX
    ```

4.  **Monitor progress**:
    In a separate terminal, type `orch-monitor` to see task status live.

---

## CLI Commands

| Command | Description |
|:---|:---|
| `orch new "description"` | Creates a new task and assigns it an ID. |
| `orch run ID` | Runs the agent loop for the given task. |
| `orch run ID --human-review` | Runs the task with your verification along the way. |
| `orch follow ID "description"`| Continues a finished task (adds new instructions). |
| `orch status` | Displays a list of tasks in the current project. |
| `orch status ID` | Displays detailed status and history of a specific task. |
| `orch reset ID` | Clears history and restores the task to NEW state (preserving description). |
| `orch remove ID` | Deletes a task from the database and its artifacts. |
| `orch-monitor` | Opens the terminal dashboard (Live View). |

---

## Data Structure (.orchestrator/)

An isolated data directory is created in each project:
```
.orchestrator/
├── orchestrator.db     ← SQLite database (tasks, history, statuses)
└── runs/               ← Logs and artifacts per-task
    └── TASK-XXXXXX/
        ├── conversation.md    ← Full record of agent "thoughts" and decisions
        ├── state.json         ← State machine state
        ├── architect_plan.json
        └── review_iter_N.json
```

---

## Configuration (Env Variables)

You can override default settings:
- `ORCH_MAX_ITERATIONS`: Round limit (default 6).
- `ORCH_ARCHITECT_RUNTIME`: Runtime for the architect (`claude` | `gemini`).
- `ORCH_ARCHITECT_MODEL`: Specific model ID for the architect.
- `ORCH_DEVELOPER_RUNTIME`: Runtime for the developer.
- `ORCH_DEVELOPER_MODEL`: Model ID for the developer.
- `ORCH_USE_GIT`: Whether to make automatic commits (default true).
