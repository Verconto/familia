"""Personal-data scanner invoked by .githooks/pre-commit.

Reads the staged diff from stdin (output of ``git diff --cached -U0``) and
the blocklist path from ``argv[1]``. Prints one violation per line to stderr
and exits 1 if any blocklist pattern (case-insensitive substring) appears in
an *added* line of the diff. Exits 0 on a clean diff.

Lives as a standalone file because the obvious heredoc form
(``python3 - <<'PY'`` ...) fights the diff pipe for stdin.
"""

from __future__ import annotations

import sys
import unicodedata


def _norm(s: str) -> str:
    """Casefold + NFKC-normalize so visually-equivalent forms collide.

    Without NFKC, an attacker (or accidental copy-paste from a font that
    normalizes differently) can sneak ``Иванов`` past a blocklist by
    composing ``И`` with combining marks or by mixing Cyrillic/Latin
    homoglyphs (``Иванoв`` with Latin ``o``). NFKC compatibility-decomposes
    those into a canonical form before the substring match.
    """
    return unicodedata.normalize("NFKC", s).casefold()


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: _scan.py <blocklist-path>\n")
        return 2
    blocklist_path = sys.argv[1]

    with open(blocklist_path, encoding="utf-8") as f:
        patterns = [
            line.strip() for line in f
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not patterns:
        return 0

    diff_bytes = sys.stdin.buffer.read()
    diff_text = diff_bytes.decode("utf-8", errors="replace")

    added: list[str] = []
    binary_files: list[str] = []
    for raw in diff_text.splitlines():
        # `git diff` annotates a binary file as e.g.:
        #   "Binary files /dev/null and b/img.png differ"
        # The substring scan can't see its content, so flag it for review.
        if raw.startswith("Binary files "):
            binary_files.append(raw)
            continue
        if raw.startswith("+++ "):
            continue
        if raw.startswith("+"):
            added.append(raw[1:])

    if binary_files:
        sys.stderr.write(
            "pre-commit: warning — staged binary file(s) NOT scanned for "
            "personal data; verify manually:\n"
        )
        for line in binary_files:
            sys.stderr.write(f"  {line}\n")

    if not added:
        return 0

    haystack = _norm("\n".join(added))
    violations = [p for p in patterns if _norm(p) in haystack]

    if violations:
        sys.stderr.write(
            "pre-commit: BLOCKED — staged changes contain personal-data tokens:\n"
        )
        for v in violations:
            sys.stderr.write(f"  - {v}\n")
        sys.stderr.write(
            f"pre-commit: scrub the diff or, if a false positive, edit {blocklist_path}\n"
            "pre-commit: bypass with --no-verify only as a last resort\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
