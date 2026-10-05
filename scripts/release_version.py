#!/usr/bin/env python3
"""Update and verify current Familia/Admin release bindings (Python 3.11+)."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from check_release_identity import check as check_backend


ROOT = Path(__file__).resolve().parents[1]
VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"


def version(value: str) -> str:
    if not re.fullmatch(VERSION, value):
        raise argparse.ArgumentTypeError("expected major.minor.patch without a prefix")
    return value


def updates(root: Path, admin: Path, backend_version: str, admin_version: str) -> dict[Path, str]:
    """Preflight every binding before returning any writes."""
    result: dict[Path, str] = {}

    def bind(path: Path, prefix: str, expected: str, count: int = 1) -> None:
        text = result.get(path, path.read_text(encoding="utf-8"))
        pattern = re.compile(f"({prefix})({VERSION})", re.MULTILINE)
        matches = list(pattern.finditer(text))
        if len(matches) != count:
            raise ValueError(f"{path}: expected {count} release bindings, found {len(matches)}")
        result[path] = pattern.sub(lambda match: match[1] + expected, text)

    identity_path = root / "release/release-identity.json"
    # Keep unrelated identity metadata and formatting untouched.
    bind(identity_path, r'"backend_version": "', backend_version)
    bind(identity_path, r'"release_tag": "image-v', backend_version)
    bind(identity_path, r'"familia": "', backend_version)
    bind(root / "familia/pyproject.toml", r'^version = "', backend_version)
    bind(root / "familia/src/familia/__init__.py", r'^__version__ = "', backend_version)
    bind(root / "Dockerfile", r'^ARG FAMILIA_VERSION=', backend_version)
    bind(root / "docker-compose.yml", r'\$\{FAMILIA_TAG:-', backend_version, 2)
    bind(root / "docker-compose.memx.yml", r'\$\{MEMX_TAG:-', backend_version, 2)
    bind(admin / "package.json", r'^  "version": "', admin_version)
    bind(admin / "src-tauri/tauri.conf.json", r'^  "version": "', admin_version)
    bind(admin / "src-tauri/Cargo.toml", r'^version = "', admin_version)
    bind(admin / "src-tauri/Cargo.lock", r'^name = "familia-admin"\nversion = "', admin_version)
    bind(admin / "README.md", r'Admin — `v', admin_version)

    for relative, admin_prefix, backend_prefix in (
        ("README.md", r'сборка админки `v', r'серверная часть `'),
        ("README.en.md", r'admin build `v', r'backend `'),
        ("docs/quickstart.md", r'Версия сборки админки \(`v', r'версия серверной части \(`'),
        ("docs/en/quickstart.md", r'Admin build version \(`v', r'backend version \(`'),
    ):
        bind(root / relative, admin_prefix, admin_version)
        bind(root / relative, backend_prefix, backend_version)

    notes = admin / "release-notes" / f"release-notes-v{admin_version}.md"
    body = notes.read_text(encoding="utf-8")
    if f"v{admin_version}" not in body or not any(line.startswith("- ") for line in body.splitlines()):
        raise ValueError(f"{notes}: prepare release notes with a version heading and changes first")
    release_versions = re.findall(r"^[^`\n]+: `(\d+\.\d+\.\d+)`\.?$", body, re.MULTILINE)
    if release_versions != [admin_version, backend_version]:
        raise ValueError(f"{notes}: release metadata must list Admin {admin_version}, then backend {backend_version}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "set"))
    parser.add_argument("--backend", type=version, help="new backend version for set")
    parser.add_argument("--admin", type=version, help="new Admin version for set")
    parser.add_argument("--admin-dir", type=Path, default=ROOT / "admin")
    args = parser.parse_args()
    if args.command == "check" and (args.backend or args.admin):
        parser.error("check reads the canonical versions; version arguments are only for set")
    if args.command == "set" and not (args.backend or args.admin):
        parser.error("set requires --backend and/or --admin")
    try:
        identity = json.loads((ROOT / "release/release-identity.json").read_text(encoding="utf-8"))
        package = json.loads((args.admin_dir / "package.json").read_text(encoding="utf-8"))
        backend_version = version(args.backend or identity["backend_version"])
        admin_version = version(args.admin or package["version"])
        planned = updates(ROOT, args.admin_dir, backend_version, admin_version)
        changed = [path for path, text in planned.items() if path.read_text(encoding="utf-8") != text]
        if args.command == "set":
            for path in changed:
                path.write_text(planned[path], encoding="utf-8", newline="\n")
                print(f"UPDATED: {path}")
            print("Run check before committing both repositories.")
            return 0
        errors = [f"release binding diverges: {path}" for path in changed]
        errors.extend(check_backend(backend_version, identity["release_tag"]))
        if errors:
            for error in errors:
                print(f"FAIL: {error}")
            return 1
        print(f"PASS: backend={backend_version} admin={admin_version}; {len(planned)} files checked")
        return 0
    except (OSError, ValueError, KeyError, argparse.ArgumentTypeError) as error:
        print(f"FAIL: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
