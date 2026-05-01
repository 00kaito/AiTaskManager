"""
prompts.py — szablony promptów dla każdej fazy orkiestratora

Każda funkcja zwraca gotowy string do wysłania do agenta.
Dane wejściowe to zawsze jawne parametry — zero globalnych.
"""

import json
from pathlib import Path


# ─────────────────────────────────────────────
# FAZA 1: ARCHITECTING  →  Claude
# ─────────────────────────────────────────────

def architect_prompt(task_description: str, codebase_summary: str) -> str:
    return f"""
You are a senior software architect. Your job is to create a precise implementation plan.

## Task
{task_description}

## Current codebase context
{codebase_summary}

## Your output — respond with ONLY valid JSON, no markdown fences, no explanation

{{
  "summary": "1-2 sentence description of the approach",
  "plan": [
    {{
      "step": 1,
      "title": "short title",
      "description": "what exactly needs to be done",
      "files_affected": ["path/to/file.py"],
      "type": "CREATE | MODIFY | DELETE"
    }}
  ],
  "acceptance_criteria": [
    {{
      "id": "c1",
      "description": "Verifiable criterion — specific, no vague language",
      "how_to_verify": "exact method: function signature, test name, file existence, etc."
    }}
  ],
  "risks": ["potential issue 1", "potential issue 2"]
}}

Rules for acceptance_criteria:
- Each criterion must be independently verifiable by reading code
- NO criteria like "code is clean" or "works correctly" — too vague
- GOOD: "Function `process_items` in `utils.py` accepts a `List[str]` and returns `List[dict]`"
- GOOD: "File `legacy_handler.py` has been deleted"
- GOOD: "All existing tests in `tests/` pass"
- Maximum 8 criteria
""".strip()


# ─────────────────────────────────────────────
# FAZA 1b: ANALYZING  →  Gemini
# ─────────────────────────────────────────────

def analyze_prompt(task_description: str, architect_plan: str, codebase_summary: str) -> str:
    return f"""
You are a senior software code analyst. The architect has created a high-level plan for a task.
Your job is to deeply analyze the codebase and enrich the plan with precise code-level details.

## Task
{task_description}

## High-level Architect Plan
{architect_plan}

## Codebase Context
{codebase_summary}

## Instructions
1. DO NOT modify any files in the project. Your job is ONLY to research and output JSON.
2. Use your search tools to find the exact files, classes, methods, and functions needed to implement the plan.
3. For each step in the plan, determine the specific `symbols_affected` and provide deep `code_hints` for the developer.
4. Respond ONLY with the updated plan in JSON format. Do not use markdown fences.

## Expected JSON Output
{{
  "plan": [
    {{
      "step": 1,
      "title": "short title",
      "description": "what exactly needs to be done",
      "files_affected": ["path/to/file.py"],
      "symbols_affected": ["ClassName", "function_name"],
      "code_hints": "Important details: which method to call, data flow, potential gotchas.",
      "type": "CREATE | MODIFY | DELETE"
    }}
  ]
}}
""".strip()


# ─────────────────────────────────────────────
# FAZA 2: IMPLEMENTING  →  Gemini
# ─────────────────────────────────────────────

def implement_prompt(
    task_description: str,
    architect_plan: str,
    open_criteria: list[dict],
    previous_diff: str = "",
    iteration: int = 1,
    fix_context: str = "",
) -> str:
    open_block = ""
    if open_criteria:
        items = "\n".join(
            f"  [{c['id']}] {c['description']}" for c in open_criteria
        )
        open_block = f"""
## ⚠️ Unfinished criteria from previous review
These were NOT done yet — focus on them:
{items}
"""

    fix_block = ""
    if fix_context:
        fix_block = f"""
## 🔧 Fix plan (based on human tester feedback)
{fix_context}
"""

    diff_block = ""
    if previous_diff:
        diff_block = f"""
## Changes from previous iteration (for context only)
```diff
{previous_diff[:3000]}
```
"""

    return f"""
You are a senior software engineer. Implement the following plan precisely.

## Task
{task_description}

## Implementation plan
{architect_plan}
{fix_block}{open_block}{diff_block}
## Instructions
- You MUST use your file editing tools to directly modify, create, or delete files in the project — do NOT just print code to the console
- Implement ONLY what is described in the plan — do not refactor unrelated code
- Iteration: {iteration}
- After finishing, write a report to the file `implementation_report.md` in the ROOT of the project (the directory you are working in, NOT in any runs/ subdirectory)
- The report must contain:
  1. What you did (bullet points per step)
  2. Which files were created / modified / deleted
  3. Any blockers or deviations from the plan (if none, write "None")

## Report format (write this to implementation_report.md)
```
# Implementation Report — Iteration {iteration}

## Changes made
- ...

## Files affected
- CREATED: path/to/file
- MODIFIED: path/to/file
- DELETED: path/to/file

## Deviations from plan
None / description

## Potential issues
None / description
```
""".strip()


# ─────────────────────────────────────────────
# FAZA 3: REVIEWING  →  Claude
# ─────────────────────────────────────────────

def review_prompt(
    task_description: str,
    criteria: list[dict],
    implementation_report: str,
    diff: str,
    iteration: int,
) -> str:
    criteria_json = json.dumps(criteria, ensure_ascii=False, indent=2)

    return f"""
You are a strict code reviewer. Your job is to verify whether the implementation meets the criteria.

## Task
{task_description}

## Iteration
{iteration}

## Acceptance criteria to verify
{criteria_json}

## Implementation report (from Gemini)
{implementation_report}

## Git diff (actual changes)
```diff
{diff[:6000]}
```

## Your output — respond with ONLY valid JSON, no markdown fences, no explanation

{{
  "iteration": {iteration},
  "overall_status": "APPROVED | CHANGES_REQUESTED",
  "criteria_results": [
    {{
      "id": "c1",
      "description": "copy from criteria",
      "status": "DONE | PENDING | FAILED",
      "evidence": "specific line/file/function that proves it, or explanation why it's missing",
      "confidence": "HIGH | MEDIUM | LOW"
    }}
  ],
  "blocking_issues": [
    "specific issue that must be fixed before APPROVED"
  ],
  "suggestions": [
    "non-blocking suggestion (optional improvements)"
  ],
  "next_focus": "1-2 sentences telling Gemini exactly what to do next (only if CHANGES_REQUESTED)"
}}

Rules:
- APPROVED only if ALL criteria have status DONE
- Be strict — "looks like it might work" is NOT evidence
- evidence must reference actual code in the diff or existing files
- If a criterion cannot be verified from the diff alone, mark it PENDING with evidence="not visible in diff, needs manual check"
- confidence LOW means you're guessing — flag it
""".strip()


# ─────────────────────────────────────────────
# FAZA HUMAN_FEEDBACK: analiza feedbacku  →  Claude
# ─────────────────────────────────────────────

def human_feedback_prompt(
    task_description: str,
    architect_plan: str,
    human_feedback: str,
    implementation_report: str,
    diff: str,
    iteration: int,
) -> str:
    return f"""
You are a senior software architect. A human tester reported that the implementation does not work correctly.

## Original task
{task_description}

## Original implementation plan
{architect_plan}

## Human tester's feedback — what doesn't work
{human_feedback}

## Gemini's implementation report (iteration {iteration})
{implementation_report}

## Git diff (actual changes)
```diff
{diff[:4000]}
```

## Your job
Analyze the human's feedback and create a precise, actionable fix plan for the developer (Gemini).

## Your output — respond with ONLY valid JSON, no markdown fences, no explanation

{{
  "root_cause": "1-2 sentences explaining why the reported issue occurs based on the diff",
  "fix_steps": [
    {{
      "step": 1,
      "description": "exactly what to change and where — be specific about function names, file paths, logic",
      "files_affected": ["path/to/file.py"]
    }}
  ],
  "key_fix": "1 sentence — the single most important thing to fix"
}}

Rules:
- Be concrete: reference actual function names, line numbers, variable names from the diff
- Do NOT repeat the original plan steps that were already done correctly
- Focus only on what's broken according to the human's feedback
""".strip()


# ─────────────────────────────────────────────
# FAZA REVIEWING (human_review mode)  →  Claude
# ─────────────────────────────────────────────

def code_quality_review_prompt(
    task_description: str,
    criteria: list[dict],
    implementation_report: str,
    diff: str,
    iteration: int,
) -> str:
    criteria_json = json.dumps(criteria, ensure_ascii=False, indent=2)

    return f"""
You are a strict code reviewer. The human tester has already verified that the implementation works correctly.
Your job is to review CODE QUALITY only — not functional correctness.

## Task
{task_description}

## Iteration
{iteration}

## Acceptance criteria (functionally verified by human — mark ALL as DONE)
{criteria_json}

## Implementation report (from Gemini)
{implementation_report}

## Git diff (actual changes)
```diff
{diff[:6000]}
```

## Your output — respond with ONLY valid JSON, no markdown fences, no explanation

{{
  "iteration": {iteration},
  "overall_status": "APPROVED | CHANGES_REQUESTED",
  "criteria_results": [
    {{
      "id": "c1",
      "description": "copy from criteria",
      "status": "DONE",
      "evidence": "human-verified: functionality confirmed by tester",
      "confidence": "HIGH"
    }}
  ],
  "blocking_issues": [
    "serious code quality issue that MUST be fixed (security hole, data corruption risk, resource leak, null pointer crash)"
  ],
  "suggestions": [
    "non-blocking suggestion (readability, edge case handling, etc.)"
  ],
  "next_focus": "1-2 sentences telling Gemini exactly what to fix (only if CHANGES_REQUESTED)"
}}

Rules:
- Mark ALL acceptance criteria as DONE — human confirmed functionality
- APPROVED if there are no blocking code quality issues
- CHANGES_REQUESTED ONLY for serious issues: security vulnerabilities, data loss risk, resource leaks, crashes under normal usage
- Do NOT request changes for style preferences, minor refactoring, or theoretical edge cases
- blocking_issues must reference specific file paths and function names from the diff
""".strip()


# ─────────────────────────────────────────────
# Helper: podsumowanie codebase do architektury
# ─────────────────────────────────────────────

def build_codebase_summary(project_root: Path, max_chars: int = 60000) -> str:
    """Zbiera strukturę plików i fragmenty kodu do kontekstu dla Claude."""
    lines = ["## File tree\n```"]

    # Pobierz nazwę folderu w którym jest skrypt, żeby go zignorować
    orchestrator_dir = Path(__file__).parent.name

    # Drzewo plików (ignoruj typowe śmieci + folder orkiestratora)
    ignore = {".git", "__pycache__", "node_modules", ".venv", "venv",
               "dist", "build", ".mypy_cache", ".pytest_cache", "runs",
               orchestrator_dir}

    for p in sorted(project_root.rglob("*")):
        if any(part in ignore for part in p.parts):
            continue
        if p.is_dir():
            continue
        rel = p.relative_to(project_root)
        lines.append(str(rel))

    lines.append("```\n")
    summary = "\n".join(lines)

    # Treść kluczowych plików (do limitu znaków)
    char_budget = max_chars - len(summary)
    code_sections = []

    priority_extensions = {".py", ".ts", ".js", ".go", ".rs", ".md", ".toml", ".yaml", ".yml"}

    for p in sorted(project_root.rglob("*")):
        if any(part in ignore for part in p.parts):
            continue
        if p.is_dir():
            continue
        if p.suffix not in priority_extensions:
            continue
        if char_budget <= 0:
            break

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            rel = p.relative_to(project_root)
            snippet = f"\n### {rel}\n```{p.suffix.lstrip('.')}\n{content[:8000]}\n```"
            if len(snippet) <= char_budget:
                code_sections.append(snippet)
                char_budget -= len(snippet)
        except Exception:
            continue

    return summary + "\n".join(code_sections)
