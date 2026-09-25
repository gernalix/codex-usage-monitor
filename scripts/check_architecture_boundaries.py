#!/usr/bin/env python3
"""Fail closed when the capsule architecture boundary is bypassed."""
from __future__ import annotations

import ast
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
CAPSULE_ROOT = ROOT / "src/codex_monitor/capsules"
ADAPTERS = {
    "codex_chat_dump_publisher.py", "codex_curated_vault.py",
    "codex_prompt_cost_query.py", "codex_session_archive.py",
    "codex_session_archive_incremental.py", "codex_task_costs.py",
    "codex_task_costs_incremental.py", "codex_usage_monitor.py",
    "codex_usage_publisher.py", "codex_usage_publisher_base.py",
    "codex_usage_publisher_legacy.py", "deploy_runtime.py",
    "github_actions_watch.py", "c2_orchestrator.py",
}


def dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def check_adapter(path: Path, tree: ast.Module) -> list[str]:
    errors: list[str] = []
    allowed = (ast.Import, ast.ImportFrom, ast.If)
    for node in tree.body:
        if isinstance(node, ast.Expr):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            call = node.value if isinstance(node.value, ast.Call) else None
            if call and dotted_name(call.func) == "sys.path.insert":
                continue
        if not isinstance(node, allowed):
            errors.append(f"{path.name}:{node.lineno}: top-level runtime logic in compatibility adapter")
    return errors


def imported_module_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.update(alias.asname or alias.name for alias in node.names)
    return roots


def check_tree(path: Path, tree: ast.Module) -> list[str]:
    errors: list[str] = []
    imported_roots = imported_module_roots(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                errors.append(f"{path}:{node.lineno}: wildcard import")
            module = node.module or ""
            if module.startswith("codex_monitor.capsules."):
                parts = module.split(".")
                if len(parts) >= 4:
                    target = parts[2]
                    resolved = path.resolve()
                    source_parts = resolved.relative_to(CAPSULE_ROOT).parts if resolved.is_relative_to(CAPSULE_ROOT) else ()
                    source = source_parts[0] if source_parts else None
                    public = len(parts) == 4 and parts[3].endswith("api")
                    package_api = len(parts) == 3 and any(alias.name.endswith("api") for alias in node.names)
                    if source and target != source and not (public or package_api):
                        errors.append(f"{path}:{node.lineno}: private cross-capsule import {module}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"globals", "locals"}:
            errors.append(f"{path}:{node.lineno}: dynamic namespace re-export")
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = dotted_name(target)
                if name and "." in name and name.split(".", 1)[0] in imported_roots:
                    errors.append(f"{path}:{node.lineno}: imported module namespace monkey-patch {name}")
    return errors


def architecture_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    paths = sorted((root / "src/codex_monitor").rglob("*.py"))
    paths.extend(root / name for name in sorted(ADAPTERS))
    for path in paths:
        if not path.is_file():
            errors.append(f"{path}: missing required runtime file")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}:{exc.lineno}: syntax error")
            continue
        errors.extend(check_tree(path, tree))
        if path.parent == root and path.name in ADAPTERS:
            errors.extend(check_adapter(path, tree))
    return errors


def main() -> int:
    errors = architecture_errors()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"ARCHITECTURE=FAIL errors={len(errors)}", file=sys.stderr)
        return 1
    print("ARCHITECTURE=PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
