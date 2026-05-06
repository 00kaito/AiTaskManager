# AI Task Orchestrator (v2.0)

A stateful task orchestrator working with **Claude Code** and **Gemini CLI**.  
Automates the full development lifecycle: **Architecture → Implementation → Review → Iteration → Success**.

The tool is **completely project-independent** — install it once and use it in any repository or directory.

---

## How it works?

The orchestrator manages three specialized AI agent roles:

1.  **Architect (Claude/Gemini)**: Analyzes the codebase, creates a detailed implementation plan in JSON format, and defines verifiable acceptance criteria.
2.  **Developer (Gemini/Claude)**: Receives the plan and modifies project files. The developer has permissions to edit code, create new files, and delete old ones. After each iteration, it generates an `implementation_report.md`.
3.  **Reviewer (Claude/Gemini)**: Checks the diff of changes against the acceptance criteria. If everything is ready — approves (`APPROVED`). If not — returns the task for fixes (`CHANGES_REQUESTED`) with specific feedback.

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

## Main Features

### 🧠 Intelligent Project Root Detection
The orchestrator automatically locates the main project directory using `git rev-parse --show-toplevel`. This allows you to call commands from any subdirectory, and the data will always go to a shared `.orchestrator/` folder in the project root.

### 👤 Human-in-the-loop (`--human-review`)
If you want full control, run:
```bash
orch run TASK-XXXXXX --human-review
```
The orchestrator will stop after each Gemini implementation and ask you if the solution works. You can then manually test the code. If you say `fail`, Claude will analyze your feedback and prepare a fix plan for Gemini.

### 📜 History and Audit (Git)
If the project is a Git repository, the orchestrator makes an automatic commit with a description after each iteration. This allows for an easy return to any stage of the agent's work.

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

### Task Continuation (Follow-up)
If a task is finished but you want to change or add something based on what has already been done:
```bash
# Add new instructions to an existing task
orch follow TASK-XXXXXX "Also write unit tests for the new function"

# Run again - Claude will analyze the feedback and Gemini will finish the job
orch run TASK-XXXXXX
```
The orchestrator will use the full task history (architect plan, previous reports, and diffs) to seamlessly continue the work.

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
- `ORCH_ARCHITECT_ROLE`: Model for the architect (`claude` | `gemini`).
- `ORCH_DEVELOPER_ROLE`: Model for the developer (`claude` | `gemini`).
- `ORCH_USE_GIT`: Whether to make automatic commits (default true).

Example:
```bash
ORCH_MAX_ITERATIONS=3 orch run TASK-XXXXXX
```
