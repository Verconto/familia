"""User-facing strings for the pending-principal flow.

Templated, no LLM — keeps the surface tiny and avoids spending tokens
on someone who hasn't been approved yet.
"""

from __future__ import annotations

PENDING_REPLY_RU = (
    "Здравствуйте! Я ассистент семьи. Вас ещё нет в моём списке "
    "доверенных пользователей. Передал заявку администратору на "
    "подтверждение — он вас увидит в админ-панели и решит. После "
    "подтверждения смогу отвечать."
)


def reply_for_pending() -> str:
    """Localized reply sent to a sender that just landed in pending."""
    return PENDING_REPLY_RU
