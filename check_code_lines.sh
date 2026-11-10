#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
branches=(cpp-only main rust-only syswatch-extension)
temporary_root=$(mktemp -d)
trap 'rm -rf "$temporary_root"' EXIT

worktree_for_branch() {
    local branch=$1 path=""
    while IFS= read -r line; do
        case "$line" in
            "worktree "*) path=${line#worktree } ;;
            "branch refs/heads/$branch") printf '%s\n' "$path"; return 0 ;;
        esac
    done < <(git -C "$repo_root" worktree list --porcelain)
    return 1
}

count_source_lines() {
    local directory=$1
    python3 - "$directory" <<'PY'
import os
import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

root = Path(sys.argv[1])
extensions = {
    ".py": "Python", ".cpp": "C++", ".cc": "C++", ".cxx": "C++",
    ".hpp": "C++", ".hh": "C++", ".hxx": "C++", ".rs": "Rust",
    ".c": "C", ".h": "C", ".sh": "Shell", ".bash": "Shell",
    ".toml": "TOML", ".json": "JSON", ".js": "JavaScript",
    ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".go": "Go", ".java": "Java", ".rb": "Ruby", ".yml": "YAML",
    ".yaml": "YAML", ".html": "HTML", ".css": "CSS", ".sql": "SQL",
    ".ps1": "PowerShell", ".cmake": "CMake", ".tcss": "CSS",
}
hash_comments = {"Python", "Shell", "TOML", "YAML", "Ruby", "PowerShell"}
slash_comments = {"C++", "C", "Rust", "JavaScript", "TypeScript", "Go", "Java", "CSS", "SQL", "CMake"}
totals = defaultdict(lambda: [0, 0, 0, 0])  # files, code, comments, blank

if (root / ".git").exists():
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    paths = (root / Path(name.decode()) for name in tracked.split(b"\0") if name)
else:
    ignored_dirs = {
        ".git", ".venv", "venv", "__pycache__", "build", "target", "dist",
        "node_modules", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    }
    paths = []
    for current, directories, filenames in os.walk(root):
        directories[:] = sorted(d for d in directories if d not in ignored_dirs)
        paths.extend(Path(current, filename) for filename in filenames)

for path in paths:
    filename = path.name
    if filename == "check_code_lines.sh":
        continue
    language = extensions.get(path.suffix.lower())
    if filename in {"Dockerfile", "Makefile"}:
        language = "Shell"
    if language is None:
        continue
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        continue

    counts = totals[language]
    counts[0] += 1
    docstring_lines = set()
    if language == "Python":
        try:
            tree = ast.parse("\n".join(lines))
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if not isinstance(
                    node,
                    (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    continue
                body = node.body
                if body and isinstance(body[0], ast.Expr):
                    value = body[0].value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        docstring_lines.update(range(value.lineno, value.end_lineno + 1))

    in_block_comment = False
    for number, line in enumerate(lines, start=1):
        remaining = line.strip()
        if not remaining:
            counts[3] += 1
            continue
        if number in docstring_lines:
            counts[2] += 1
            continue
        if language in hash_comments and remaining.startswith("#"):
            counts[2] += 1
            continue
        if language in slash_comments:
            while remaining:
                if in_block_comment:
                    end = remaining.find("*/")
                    if end < 0:
                        remaining = ""
                        break
                    remaining = remaining[end + 2 :].strip()
                    in_block_comment = False
                    continue
                if remaining.startswith("//"):
                    remaining = ""
                elif remaining.startswith("/*"):
                    end = remaining.find("*/", 2)
                    if end < 0:
                        in_block_comment = True
                        remaining = ""
                    else:
                        remaining = remaining[end + 2 :].strip()
                else:
                    break
            if not remaining:
                counts[2] += 1
                continue
        counts[1] += 1

print(f"{'Language':<14} {'Files':>7} {'Code':>10} {'Comments':>10} {'Blank':>10}")
for language, (files, code, comments, blank) in sorted(totals.items()):
    print(f"{language:<14} {files:>7} {code:>10} {comments:>10} {blank:>10}")
print("-" * 55)
print(
    f"{'Total':<14} {sum(v[0] for v in totals.values()):>7} "
    f"{sum(v[1] for v in totals.values()):>10} "
    f"{sum(v[2] for v in totals.values()):>10} "
    f"{sum(v[3] for v in totals.values()):>10}"
)
print("Counts tracked and non-ignored untracked files; blanks, comments, and Python docstrings are excluded.")
print("Inline comments remain part of code lines.")
print("check_code_lines.sh is excluded from the totals.")
PY
}

for branch in "${branches[@]}"; do
    if directory=$(worktree_for_branch "$branch"); then
        echo "=== $branch ($directory) ==="
    else
        directory="$temporary_root/$branch"
        mkdir -p "$directory"
        git -C "$repo_root" archive "$branch" | tar -x -C "$directory"
        echo "=== $branch (archived branch snapshot) ==="
    fi
    count_source_lines "$directory"
done
