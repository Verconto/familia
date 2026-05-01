Update memory files / scoped memX based on the analysis below.
- [FILE] entries: add the described content to the appropriate file
- [FILE-REMOVE] entries: delete the corresponding content from memory files
- [PRIVATE:<actor>] entries: call `dream_memory_set` with scope='private',
  actor='<actor>', key='<stable_key>', value='<content>'.
  Pick a stable key from MEMORY_KEYS.md conventions (e.g. `feelings`,
  `work_context`, `daily_routine`) so repeated dreams overwrite the same slot
  rather than accumulating forever.  memX is last-write-wins.
- [PAIR:<a>,<b>] entries: call `dream_memory_set` with scope='pair',
  actor='<a>', other='<b>', key='<stable_key>', value='<content>'.
- [SKILL] entries: create a new skill under skills/<name>/SKILL.md using write_file

## File paths (relative to workspace root)
- SOUL.md
- USER.md
- memory/MEMORY.md
- skills/<name>/SKILL.md (for [SKILL] entries only)

Do NOT guess paths.

## Privacy invariant
Never copy a [PRIVATE:<actor>] or [PAIR:…] fact into MEMORY.md / USER.md / SOUL.md.
MEMORY.md is read by the agent in every session (including sessions with other
principals); shared files must not carry private-to-one-principal content.

## Editing rules
- Edit directly — file contents provided below, no read_file needed
- Use exact text as old_text, include surrounding blank lines for unique match
- Batch changes to the same file into one edit_file call
- For deletions: section header + all bullets as old_text, new_text empty
- Surgical edits only — never rewrite entire files
- If nothing to update, stop without calling tools

## Skill creation rules (for [SKILL] entries)
- Use write_file to create skills/<name>/SKILL.md
- Before writing, read_file `{{ skill_creator_path }}` for format reference (frontmatter structure, naming conventions, quality standards)
- **Dedup check**: read existing skills listed below to verify the new skill is not functionally redundant. Skip creation if an existing skill already covers the same workflow.
- Include YAML frontmatter with name and description fields
- Keep SKILL.md under 2000 words — concise and actionable
- Include: when to use, steps, output format, at least one example
- Do NOT overwrite existing skills — skip if the skill directory already exists
- Reference specific tools the agent has access to (read_file, write_file, exec, web_search, etc.)
- Skills are instruction sets, not code — do not include implementation code

## Quality
- Every line must carry standalone value
- Concise bullets under clear headers
- When reducing (not deleting): keep essential facts, drop verbose details
- If uncertain whether to delete, keep but add "(verify currency)"
