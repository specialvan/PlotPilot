"""Measure tracked Python dependencies without importing application modules.

Counts describe source references, not architecture violations or runtime calls.
Relative imports, dynamic imports and alias-based bypass calls are not resolved.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
LAYERS = ("domain", "application", "engine", "infrastructure", "interfaces")
BYPASS_FUNCTIONS = {"sqlite_writes_bypass_queue", "startup_sqlite_writes_bypass_queue"}


class DependencyVisitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.layer = path.split("/")[0]
        self.function_depth = 0
        self.type_checking_depth = 0
        self.imports: list[dict] = []
        self.bypass_calls: list[dict] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        # Only the two literal guards below are classified as type-checking imports.
        guarded = ast.unparse(node.test) in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}
        self.visit(node.test)
        self.type_checking_depth += int(guarded)
        for child in node.body:
            self.visit(child)
        self.type_checking_depth -= int(guarded)
        for child in node.orelse:
            self.visit(child)

    def _record(self, node: ast.Import | ast.ImportFrom, target: str) -> None:
        if target in LAYERS and target != self.layer:
            self.imports.append(
                {
                    "path": self.path,
                    "line": node.lineno,
                    "source_layer": self.layer,
                    "target_layer": target,
                    "statement": ast.unparse(node),
                    "in_function": self.function_depth > 0,
                    "type_checking": self.type_checking_depth > 0,
                }
            )

    def visit_Import(self, node: ast.Import) -> None:
        # Multiple imported names in one target layer count as one statement.
        for target in sorted({alias.name.split(".")[0] for alias in node.names}):
            self._record(node, target)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self._record(node, node.module.split(".")[0])

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in BYPASS_FUNCTIONS:
            self.bypass_calls.append(
                {"path": self.path, "line": node.lineno, "function": node.func.id}
            )
        self.generic_visit(node)


def analyze() -> dict:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    )
    imports: list[dict] = []
    bypass_calls: list[dict] = []
    files: list[dict] = []
    for entry in sorted(part for part in result.stdout.split(b"\0") if part):
        path = entry.decode("utf-8")
        if path.split("/")[0] not in LAYERS:
            continue
        source = (REPO_ROOT / path).read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=path)
        visitor = DependencyVisitor(path)
        visitor.visit(tree)
        imports.extend(visitor.imports)
        bypass_calls.extend(visitor.bypass_calls)
        files.append(
            {
                "path": path,
                "lines": len(source.splitlines()),
                "functions": sum(
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for node in ast.walk(tree)
                ),
            }
        )
    edges = []
    for source_layer in LAYERS:
        for target_layer in LAYERS:
            if source_layer == target_layer:
                continue
            references = [
                item for item in imports
                if item["source_layer"] == source_layer and item["target_layer"] == target_layer
            ]
            edges.append(
                {
                    "from": source_layer,
                    "to": target_layer,
                    "statements": len(references),
                    "in_functions": sum(item["in_function"] for item in references),
                    "type_checking": sum(item["type_checking"] for item in references),
                }
            )
    return {
        "tracked_python_files": len(files),
        "edges": edges,
        "hotspots": sorted(files, key=lambda item: (-item["lines"], item["path"]))[:15],
        "bypass_calls": bypass_calls,
        "imports": imports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Include every reference and source line.")
    args = parser.parse_args()
    try:
        result = analyze()
    except (OSError, UnicodeError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"Architecture scan failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"Scanned {result['tracked_python_files']} tracked Python files; source references only.")
    print("| From | To | Import statements | In functions | TYPE_CHECKING |")
    print("| --- | --- | ---: | ---: | ---: |")
    for edge in result["edges"]:
        print(
            f"| {edge['from']} | {edge['to']} | {edge['statements']} "
            f"| {edge['in_functions']} | {edge['type_checking']} |"
        )
    counts = Counter(item["function"] for item in result["bypass_calls"])
    for function in sorted(BYPASS_FUNCTIONS):
        print(f"{function}: {counts[function]} direct call expressions")
    print("| File | Lines | Function definitions |")
    print("| --- | ---: | ---: |")
    for item in result["hotspots"]:
        print(f"| {item['path']} | {item['lines']} | {item['functions']} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
