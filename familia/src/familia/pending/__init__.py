"""Pending-principal approval flow.

When an unknown ``(channel, sender_id)`` writes to the bot, the agent
loop intercepts before any LLM/tool work, drops the row into
``~/.nanobot/pending_principals.json``, and replies with a templated
"awaiting admin approval" message. The admin app surfaces the list
and lets a human approve or reject.

Approve mutates ``principals.json`` (atomic). Reject drops the row
and silences the same sender for ``REJECT_COOLDOWN_SECS`` so a single
misclick doesn't permanently lock somebody out, but a flooder isn't
re-noticed every message.
"""
