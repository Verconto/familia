"""familia — family-assistant extensions for the nanobot agent loop.

Public surface:

* :func:`familia.bootstrap.install_tools` — register familia tools on an
  ``AgentLoop`` at construction time.
* :func:`familia.bootstrap.on_inbound` — set per-turn actor/role context
  when an inbound message is dispatched.
* :mod:`familia.principals`, :mod:`familia.policy`, :mod:`familia.roles` —
  imported directly by policy-aware upstream touchpoints
  (``tools.message``, ``bus.events.InboundMessage.actor``, etc.).

Everything else is an implementation detail.
"""

__version__ = "0.1.0"
