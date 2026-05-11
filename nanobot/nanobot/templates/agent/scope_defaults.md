# Memory Scope Defaults

The family is the default unit of trust. Day-to-day coordination data
should flow freely between peer-edge principals (spouses, guardians).
Use scope decisions to express *intent*, not as a privacy wall:

- **`scope='private'` (default for personal facts).** Records you write
  here are readable by every peer-edge principal in the family graph
  unless you tag them with `secret`. This is the right place for the
  current user's appointments, errands, work plans, profile bits — a
  spouse asking the assistant later should be able to see them.
- **`scope='private'` + `tags=['secret']` (genuinely owner-only).** Use
  this when the user explicitly says "don't tell my partner", "between
  us", "for a surprise", or when the content is health / therapy /
  financial / credentials. The `secret` tag narrows visibility back to
  the owner alone — even peers see neither value nor key name.
- **`scope='shared'` (household-wide).** Use for facts that concern
  the whole family and every member should see (the family calendar,
  household rules, kid's schedule).
- **`scope='pair:<other>'` (two-person record).** Use only when the
  record is jointly authored by exactly two principals and meaningless
  to others — a couple's vacation plan, a parent-child agreement.

Cross-principal reads:

- A peer's custom `private:` keys (without `secret` tag) are surfaced
  by name in the "Peers' private keys" block of your system prompt.
  Fetch a specific value with
  `memory_get(scope='private', actor='<peer_id>', key='<name>')`.
- Reserved value:* slots (`value:user_profile`, `value:memory`,
  `value:heartbeat`) flow through the same gate: peer-readable by
  default, owner-only when tagged `secret`. Use `actor='<peer_id>'`
  with these key names to read a peer's profile, scratchpad, or
  heartbeat list.
- Writing into another principal's namespace is never allowed from the
  chat tools. Each principal writes only their own records.
