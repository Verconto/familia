"""Standalone scanner: walk tracked + untracked files and report blocklist hits.

Unlike _scan.py (staged-diff only), this scans the whole working tree
including files git is about to commit, helps verify cleanliness before
the first audit. Skips .gitignored files. Run:

    python3 .githooks/_scan_tree.py [.githooks/blocklist]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def main() -> int:
    blocklist_path = sys.argv[1] if len(sys.argv) > 1 else ".githooks/blocklist"
    with open(blocklist_path, encoding="utf-8") as f:
        patterns = [
            line.strip() for line in f
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not patterns:
        print("scan: blocklist empty — nothing to do")
        return 0

    folded_patterns = [(p, p.casefold()) for p in patterns]

    files = _git_files()
    hits: dict[str, list[tuple[int, str, str]]] = {}
    skipped: list[str] = []
    for path in files:
        if path == blocklist_path or path == ".githooks/blocklist":
            continue
        try:
            data = Path(path).read_bytes()
        except OSError:
            skipped.append(path)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append(path)
            continue
        folded_lines = [(i, ln, ln.casefold()) for i, ln in enumerate(text.splitlines(), 1)]
        for orig_pat, fp in folded_patterns:
            for lineno, raw, low in folded_lines:
                if fp in low:
                    hits.setdefault(path, []).append((lineno, orig_pat, raw[:140]))

    if not hits:
        print(f"scan: clean ({len(files)} files, {len(patterns)} patterns)")
        if skipped:
            print(f"scan: skipped {len(skipped)} non-utf8/unreadable files")
        return 0

    for path in sorted(hits):
        for lineno, pat, raw in hits[path]:
            print(f"{path}:{lineno}: matched '{pat}' -> {raw}")
    print(f"scan: {sum(len(v) for v in hits.values())} hit(s) in {len(hits)} file(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
