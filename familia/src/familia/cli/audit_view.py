#!/usr/bin/env python3
"""Audit log viewer for familia.

Reads JSONL from $FAMILIA_AUDIT_FILE (default ``./audit.jsonl`` in cwd —
matches ``familia.audit``'s default for local dev). The compose stack
sets FAMILIA_AUDIT_FILE to the container-absolute path.

Usage:
    familia-audit                          # last 20 entries
    familia-audit --kind policy
    familia-audit --actor member_a
    familia-audit --decision deny,ask
    familia-audit --since 1h               # 30m / 2h / 1d
    familia-audit --tail                   # follow mode
    familia-audit --stats                  # counts per (kind, decision)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("FAMILIA_AUDIT_FILE", "audit.jsonl"))

COLOR = {
    "allow": "\033[32m",
    "deny": "\033[31m",
    "ask": "\033[33m",
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


def parse_since(s: str) -> datetime:
    units = {"m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        return datetime.now(timezone.utc) - timedelta(seconds=int(s[:-1]) * units[s[-1]])
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def entry_ts(e: dict) -> datetime | None:
    ts = e.get("ts")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def match(e: dict, args) -> bool:
    if args.kind and e.get("kind") not in args.kind:
        return False
    if args.actor and e.get("actor") not in args.actor:
        return False
    if args.decision and e.get("decision") not in args.decision:
        return False
    if args.action and e.get("action") not in args.action:
        return False
    if args.since:
        ts = entry_ts(e)
        if ts is None or ts < args.since:
            return False
    return True


def fmt(e: dict, color: bool) -> str:
    ts = e.get("ts", "")[:19].replace("T", " ")
    kind = e.get("kind", "?")
    actor = e.get("actor") or "-"
    action = e.get("action", "")
    decision = e.get("decision", "")
    to = e.get("to_chat") or e.get("chat_id") or ""
    rule = e.get("rule", "")
    reason = e.get("reason", "")

    dcol = COLOR.get(decision, "") if color else ""
    rst = COLOR["reset"] if color else ""
    dim = COLOR["dim"] if color else ""

    head = f"{dim}{ts}{rst} {kind:<10} {actor:<12} {action:<16} → {to:<20}"
    if decision:
        head += f" {dcol}{decision}{rst}"
    tail = ""
    if rule and rule != "__default_deny__":
        tail += f"  [{rule}]"
    if reason:
        tail += f"  — {reason}"
    return head + dim + tail + rst


def iter_lines(path: Path, follow: bool):
    if not path.exists():
        print(f"audit file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            yield line
        if follow:
            while True:
                where = f.tell()
                line = f.readline()
                if not line:
                    time.sleep(0.5)
                    f.seek(where)
                    continue
                yield line


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--path", type=Path, default=DEFAULT_PATH)
    p.add_argument("--kind", type=lambda s: s.split(","))
    p.add_argument("--actor", type=lambda s: s.split(","))
    p.add_argument("--decision", type=lambda s: s.split(","))
    p.add_argument("--action", type=lambda s: s.split(","))
    p.add_argument("--since", type=parse_since)
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--tail", action="store_true", help="follow new entries")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--no-color", action="store_true")
    args = p.parse_args()

    color = not args.no_color and sys.stdout.isatty()

    entries = []
    for line in iter_lines(args.path, follow=args.tail):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not match(e, args):
            continue
        if args.tail:
            print(fmt(e, color))
            sys.stdout.flush()
        else:
            entries.append(e)

    if args.tail:
        return 0

    if args.stats:
        by_kind = Counter(e.get("kind") for e in entries)
        by_decision = Counter(
            (e.get("kind"), e.get("decision")) for e in entries if e.get("decision")
        )
        by_actor = Counter(e.get("actor") or "-" for e in entries)
        print(f"total: {len(entries)}")
        print("\nby kind:")
        for k, n in by_kind.most_common():
            print(f"  {k:<12} {n}")
        print("\nby actor:")
        for a, n in by_actor.most_common():
            print(f"  {a:<14} {n}")
        print("\nby (kind, decision):")
        for (k, d), n in by_decision.most_common():
            dcol = COLOR.get(d, "") if color else ""
            rst = COLOR["reset"] if color else ""
            print(f"  {k:<10} {dcol}{d:<6}{rst} {n}")
        return 0

    tail_n = entries[-args.limit:] if args.limit > 0 else entries
    for e in tail_n:
        print(fmt(e, color))
    return 0


if __name__ == "__main__":
    sys.exit(main())
