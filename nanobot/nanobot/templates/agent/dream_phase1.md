You have TWO equally important tasks:
1. Extract new facts from conversation history
2. Deduplicate existing memory files — find and flag redundant, overlapping, or stale content even if NOT mentioned in history

Each conversation entry is tagged with `actor=<principal_id>` — this is the
principal whose turn produced the summary. `(untagged)` means the entry is
from before per-scope tagging or from a system/mixed source.

Output one line per finding:
[FILE] atomic fact (not already in memory)               # MEMORY/USER/SOUL — family-wide
[FILE-REMOVE] reason for removal
[PRIVATE:<actor>] atomic fact private to <actor>         # goes to memX private:<actor>:*
[PAIR:<a>,<b>] atomic fact shared by <a> and <b> only    # goes to memX pair:<a>_<b>:*
[SKILL] kebab-case-name: one-line description of the reusable pattern

Scope routing — CRITICAL to prevent leaks between principals:
- [FILE]  — only facts the whole family knows or needs (shared logistics, pet names,
            common routines). NEVER put here something only one principal said in
            private (medical details, work secrets, personal feelings towards
            another principal).
- [PRIVATE:<actor>] — a fact mentioned by one principal that should NOT leak to
            others when the agent talks to them. Example:
            [PRIVATE:alex] concerned about work deadline next week
- [PAIR:<a>,<b>] — a fact relevant only to a specific pair (e.g. spouses
            planning a gift for a third person). Sort actor ids alphabetically.
- When in doubt — prefer PRIVATE over FILE. Over-sharing is the worse failure mode.

Files: USER (identity, preferences), SOUL (bot behavior, tone), MEMORY (knowledge, project context)

Rules:
- Atomic facts: "has a cat named Luna" not "discussed pet care"
- Corrections: [USER] location is Tokyo, not Osaka
- Capture confirmed approaches the user validated

Deduplication — scan ALL memory files for these redundancy patterns:
- Same fact stated in multiple places (e.g., "communicates in Chinese" in both USER.md and multiple MEMORY.md entries)
- Overlapping or nested sections covering the same topic
- Information in MEMORY.md that is already captured in USER.md or SOUL.md (MEMORY.md should not duplicate permanent-file content)
- Verbose entries that can be condensed without losing information
For each duplicate found, output [FILE-REMOVE] for the less authoritative copy (prefer keeping facts in their canonical location)

Staleness — MEMORY.md lines may have a ``← Nd`` suffix showing days since last modification:
- SOUL.md and USER.md have no age annotations — they are permanent, only update with corrections
- Age only indicates when content was last touched, not whether it should be removed
- Use content judgment: user habits/preferences/personality traits are permanent regardless of age
- Only prune content that is objectively outdated: passed events, resolved tracking, superseded approaches
- Lines with ``← Nd`` (N>{{ stale_threshold_days }}) deserve closer review but are NOT automatically removable
- When removing: prefer deleting individual items over entire sections

Skill discovery — flag [SKILL] when ALL of these are true:
- A specific, repeatable workflow appeared 2+ times in the conversation history
- It involves clear steps (not vague preferences like "likes concise answers")
- It is substantial enough to warrant its own instruction set (not trivial like "read a file")
- Do not worry about duplicates — the next phase will check against existing skills

Do not add: current weather, transient status, temporary errors, conversational filler.

[SKIP] if nothing needs updating.
