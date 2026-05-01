"""
monitor.py — live dashboard stanu zadań w terminalu

Użycie:
    python monitor.py              # odświeża co 3s
    python monitor.py TASK-001    # śledź konkretne zadanie
    python monitor.py --once      # jednorazowy print i wyjdź
"""

import json
import sys
import time
from pathlib import Path

from config import config
from state import TaskRepository, TaskStatus

# Spróbuj użyć `rich` jeśli dostępny; fallback na plain text
try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich import box
    RICH = True
except ImportError:
    RICH = False


REFRESH_INTERVAL = 3  # sekundy

STATUS_COLORS = {
    TaskStatus.NEW: "dim white",
    TaskStatus.ARCHITECTING: "cyan",
    TaskStatus.ANALYZING: "magenta",
    TaskStatus.IMPLEMENTING: "yellow",
    TaskStatus.REVIEWING: "blue",
    TaskStatus.CHANGES_REQUESTED: "orange3",
    TaskStatus.APPROVED: "green",
    TaskStatus.STUCK: "red",
    TaskStatus.FAILED: "bright_red",
}

STATUS_ICONS = {
    TaskStatus.NEW: "○",
    TaskStatus.ARCHITECTING: "◈",
    TaskStatus.ANALYZING: "▤",
    TaskStatus.IMPLEMENTING: "◆",
    TaskStatus.REVIEWING: "◇",
    TaskStatus.CHANGES_REQUESTED: "↻",
    TaskStatus.APPROVED: "✓",
    TaskStatus.STUCK: "⚠",
    TaskStatus.FAILED: "✗",
}


# ─────────────────────────────────────────────
# Rich dashboard
# ─────────────────────────────────────────────

def build_table(repo: TaskRepository, filter_id: str = None):
    """Buduje rich.Table z aktualnym stanem zadań."""
    tasks = repo.list_all()
    if filter_id:
        tasks = [t for t in tasks if t.task_id == filter_id]

    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold white",
        border_style="dim white",
        expand=True,
    )

    table.add_column("ID", style="bold", min_width=12)
    table.add_column("Title", min_width=30)
    table.add_column("Status", min_width=20)
    table.add_column("Iter", justify="center", min_width=5)
    table.add_column("Criteria", justify="center", min_width=10)
    table.add_column("Stuck", justify="center", min_width=6)
    table.add_column("Last review note", min_width=40)

    for t in tasks:
        done = sum(1 for c in t.criteria if c["status"] == "DONE")
        total = len(t.criteria)
        crit_str = f"{done}/{total}" if total else "—"

        color = STATUS_COLORS.get(t.status, "white")
        icon = STATUS_ICONS.get(t.status, "?")
        status_text = Text(f"{icon}  {t.status.value}", style=color)

        title = t.title[:30] + "..." if len(t.title) > 30 else t.title

        last_note = "—"
        if t.history:
            last_note = t.history[-1].get("notes", "") or "—"
            if len(last_note) > 60:
                last_note = last_note[:57] + "..."

        stuck_str = str(t.stuck_counter) if t.stuck_counter > 0 else "—"
        stuck_style = "red bold" if t.stuck_counter >= 1 else "dim"

        table.add_row(
            t.task_id,
            title,
            status_text,
            str(t.iteration),
            crit_str,
            Text(stuck_str, style=stuck_style),
            last_note,
        )

    return table


def build_detail_panel(repo: TaskRepository, task_id: str):
    """Panel ze szczegółami jednego zadania — kryteria i historia."""
    task = repo.load(task_id)
    if not task:
        return Panel(f"Task {task_id} not found", title="Error")

    lines = []

    # Kryteria
    lines.append("[bold white]Criteria:[/bold white]")
    for c in task.criteria:
        icon = {"DONE": "[green]✓[/green]", "PENDING": "[yellow]○[/yellow]",
                "FAILED": "[red]✗[/red]"}.get(c["status"], "?")
        evidence = c.get("evidence") or ""
        if evidence and len(evidence) > 70:
            evidence = evidence[:67] + "..."
        lines.append(f"  {icon} [{c['id']}] {c['description']}")
        if evidence:
            lines.append(f"       [dim]{evidence}[/dim]")

    # Historia
    if task.history:
        lines.append("\n[bold white]Iteration history:[/bold white]")
        for h in task.history[-4:]:  # ostatnie 4
            passed = "[green]PASS[/green]" if h.get("review_passed") else "[red]FAIL[/red]"
            open_c = ", ".join(h.get("open_criteria", [])) or "none"
            lines.append(f"  iter {h['iteration']}: {passed}  open=[{open_c}]")
            if h.get("notes"):
                note = h["notes"][:80] + ("..." if len(h["notes"]) > 80 else "")
                lines.append(f"    [dim]{note}[/dim]")

    color = STATUS_COLORS.get(task.status, "white")
    icon = STATUS_ICONS.get(task.status, "?")
    title = (
        f"[{color}]{icon}  {task.task_id} — {task.status.value}[/{color}]  "
        f"[dim]iter {task.iteration}/{task.max_iterations}[/dim]"
    )

    return Panel("\n".join(lines), title=title, border_style=color)


def run_rich(filter_id: str = None, once: bool = False):
    console = Console()
    repo = TaskRepository()

    def render():
        from rich.console import Group
        parts = []

        if filter_id:
            parts.append(build_detail_panel(repo, filter_id))

        tbl = build_table(repo, filter_id if not filter_id else None)
        if not filter_id:
            parts.append(Panel(tbl, title="[bold]AI Orchestrator — Task Monitor[/bold]",
                               border_style="dim white"))
        else:
            parts.append(tbl)

        ts = time.strftime("%H:%M:%S")
        parts.append(Text(f"  refreshed {ts}  •  Ctrl+C to exit", style="dim"))
        return Group(*parts)

    if once:
        console.print(render())
        return

    with Live(render(), console=console, refresh_per_second=1,
              screen=True, transient=False) as live:
        try:
            while True:
                time.sleep(REFRESH_INTERVAL)
                live.update(render())
        except KeyboardInterrupt:
            pass


# ─────────────────────────────────────────────
# Plain text fallback
# ─────────────────────────────────────────────

def run_plain(filter_id: str = None, once: bool = False):
    repo = TaskRepository()

    def print_status():
        tasks = repo.list_all()
        if filter_id:
            tasks = [t for t in tasks if t.task_id == filter_id]

        print(f"\n{'─'*100}")
        print(f"  AI Orchestrator Monitor  {time.strftime('%H:%M:%S')}")
        print(f"{'─'*100}")
        print(f"  {'ID':<14} {'TITLE':<32} {'STATUS':<22} {'ITER':<5} {'CRIT':<8} {'STUCK'}")
        print(f"  {'─'*13} {'─'*31} {'─'*21} {'─'*4} {'─'*7} {'─'*5}")

        for t in tasks:
            done = sum(1 for c in t.criteria if c["status"] == "DONE")
            total = len(t.criteria)
            crit_str = f"{done}/{total}" if total else "—"
            icon = STATUS_ICONS.get(t.status, "?")
            stuck = str(t.stuck_counter) if t.stuck_counter else "—"
            title = t.title[:30] + "..." if len(t.title) > 30 else t.title
            print(
                f"  {t.task_id:<14} {title:<32} {icon} {t.status.value:<20} "
                f"{t.iteration:<5} {crit_str:<8} {stuck}"
            )

        if filter_id:
            task = repo.load(filter_id)
            if task and task.criteria:
                print(f"\n  Criteria:")
                for c in task.criteria:
                    icon = {"DONE": "✓", "PENDING": "○", "FAILED": "✗"}.get(
                        c["status"], "?"
                    )
                    print(f"    {icon} [{c['id']}] {c['description']}")
                    if c.get("evidence"):
                        print(f"         {c['evidence'][:70]}")

        print()

    if once:
        print_status()
        return

    try:
        while True:
            print_status()
            time.sleep(REFRESH_INTERVAL)
    except KeyboardInterrupt:
        print("\nMonitor stopped.")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    once = "--once" in args
    args = [a for a in args if a != "--once"]
    filter_id = args[0] if args else None

    if RICH:
        run_rich(filter_id, once)
    else:
        run_plain(filter_id, once)


if __name__ == "__main__":
    main()
