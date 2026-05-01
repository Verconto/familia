"""Tests for .githooks/_scan.py — the personal-data scan in pre-commit.

Run from repo root: ``python -m pytest .githooks/test_scan.py``.
"""

from __future__ import annotations

import io
import sys
import unicodedata
from pathlib import Path

import pytest


HOOK_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HOOK_DIR))
import _scan  # type: ignore  # noqa: E402


@pytest.fixture
def blocklist_file(tmp_path: Path) -> Path:
    p = tmp_path / "blocklist"
    p.write_text("# comment\nИванов\nsecret_token\n", encoding="utf-8")
    return p


def _run(diff_text: str, blocklist: Path) -> tuple[int, str]:
    monkey_argv = ["_scan.py", str(blocklist)]
    old_stdin, old_stderr, old_argv = sys.stdin, sys.stderr, sys.argv
    sys.stdin = io.TextIOWrapper(io.BytesIO(diff_text.encode("utf-8")))
    err_buf = io.StringIO()
    sys.stderr = err_buf
    sys.argv = monkey_argv
    try:
        rc = _scan.main()
    finally:
        sys.stdin, sys.stderr, sys.argv = old_stdin, old_stderr, old_argv
    return rc, err_buf.getvalue()


def _diff(added_lines: list[str]) -> str:
    """Mimic `git diff --cached -U0` framing: header + +++ + per-line +."""
    body = "\n".join(f"+{line}" for line in added_lines)
    return (
        "diff --git a/x b/x\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/x\n"
        "@@ -0,0 +1 @@\n"
    ) + body


def test_clean_diff_passes(blocklist_file):
    rc, _ = _run(_diff(["just clean text"]), blocklist_file)
    assert rc == 0


def test_obvious_match_blocks(blocklist_file):
    rc, err = _run(_diff(["leak: Иванов here"]), blocklist_file)
    assert rc == 1
    assert "BLOCKED" in err


def test_case_insensitive_match(blocklist_file):
    rc, _ = _run(_diff(["leak: ИВАНОВ here"]), blocklist_file)
    assert rc == 1


def test_nfkc_normalization_catches_decomposed_form(tmp_path: Path):
    """A blocklist composed-form name must catch its NFD-decomposed cousin.

    Use 'André' as the blocklist entry — 'é' has a single-codepoint composed
    form (U+00E9) and a multi-codepoint NFD form (e + U+0301). Without NFKC
    normalization, the plain casefold/in check misses the decomposed form.
    """
    bl = tmp_path / "blocklist"
    bl.write_text("André\n", encoding="utf-8")
    nfd = unicodedata.normalize("NFD", "André")
    assert nfd != "André"  # actually decomposed
    rc, err = _run(_diff([f"sneaky: {nfd}"]), bl)
    assert rc == 1
    assert "BLOCKED" in err


def test_binary_file_warning_emitted(blocklist_file, tmp_path: Path):
    diff = (
        "diff --git a/img.png b/img.png\n"
        "new file mode 100644\n"
        "Binary files /dev/null and b/img.png differ\n"
    )
    rc, err = _run(diff, blocklist_file)
    # No textual addition → exit 0 even though scanner has nothing to grep,
    # but the warning is emitted so the human can sanity-check.
    assert rc == 0
    assert "binary" in err.lower()
    assert "img.png" in err


def test_empty_blocklist_short_circuits(tmp_path: Path):
    bl = tmp_path / "empty"
    bl.write_text("# only comments\n\n", encoding="utf-8")
    rc, _ = _run(_diff(["Иванов here, but no patterns to check"]), bl)
    assert rc == 0


def test_only_added_lines_scanned(blocklist_file):
    """Context lines (no leading +) and deletions (-) must NOT trigger."""
    diff = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1,2 +1,1 @@\n"
        "-Иванов here   # deletion — should be ignored\n"
        " context line\n"
        "+clean replacement\n"
    )
    rc, _ = _run(diff, blocklist_file)
    assert rc == 0
