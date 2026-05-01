"""Regression tests for cron list/remove ownership-based visibility.

Closes the leak where any non-admin actor calling ``cron list`` saw every
scheduled job in the household — including reminders addressed to other
participants. After the fix:

  * Each new job stamps ``payload.created_by`` with the calling actor.
  * ``cron list`` filters to: own jobs + jobs addressed to me. Admins see all.
  * System jobs (``payload.kind == "system_event"``) are admin-only.
  * ``cron remove`` enforces the same ownership rule (with 404 on non-owner
    attempts to avoid existence leak).
  * Standalone nanobot (no getter callbacks wired) keeps the legacy
    "show everything" behavior — backwards-compat for single-user installs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.agent.tools.cron import CronTool
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronPayload, CronSchedule


SESSION_CHAT_MEMBER_A = "1000002"
CHAT_OWNER = "1000001"


@pytest.fixture
def svc(tmp_path) -> CronService:
    s = CronService(tmp_path / "jobs.json")
    s._running = True  # bypass the lazy-action queue for direct add/list/get
    return s


def _seed(svc: CronService, **kwargs: Any) -> CronJob:
    """Seed a job directly into the store."""
    job = CronJob(
        id=kwargs.get("id", "j1"),
        name=kwargs["name"],
        schedule=kwargs.get(
            "schedule", CronSchedule(kind="every", every_ms=60000)
        ),
        payload=CronPayload(
            kind=kwargs.get("kind", "agent_turn"),
            message=kwargs.get("message", ""),
            channel=kwargs.get("channel", "vk"),
            to=kwargs.get("to"),
            created_by=kwargs.get("created_by"),
        ),
        state=CronJobState(next_run_at_ms=1000),
    )
    store = svc._load_store()
    store.jobs.append(job)
    svc._save_store()
    return job


def _make_tool(
    svc: CronService,
    *,
    actor: str | None,
    admins: set[str],
    session_chat: str = SESSION_CHAT_MEMBER_A,
) -> CronTool:
    tool = CronTool(
        svc,
        default_timezone="UTC",
        current_actor_getter=lambda: actor,
        is_admin_getter=lambda a: a in admins,
    )
    tool.set_context("vk", session_chat)
    return tool


def _run(tool: CronTool, **kwargs: Any) -> str:
    return asyncio.run(tool.execute(**kwargs))


# ---- list filtering --------------------------------------------------------

def test_non_admin_sees_only_own_and_addressed_jobs(svc):
    _seed(svc, id="own1", name="member_a's reminder",
          created_by="member_a", to=SESSION_CHAT_MEMBER_A)
    _seed(svc, id="addr1", name="addressed to me by other",
          created_by="owner", to=SESSION_CHAT_MEMBER_A)
    _seed(svc, id="other1", name="owner's private note",
          created_by="owner", to=CHAT_OWNER)
    _seed(svc, id="sys1", name="dream", kind="system_event",
          created_by=None, to=None)

    tool = _make_tool(svc, actor="member_a", admins={"owner"})
    out = _run(tool, action="list")
    assert "member_a's reminder" in out
    assert "addressed to me by other" in out
    assert "owner's private note" not in out
    assert "dream" not in out
    assert "2 more hidden" in out


def test_admin_sees_all_jobs_including_system(svc):
    _seed(svc, id="z1", name="member_a's note",
          created_by="member_a", to=SESSION_CHAT_MEMBER_A)
    _seed(svc, id="v1", name="owner's note",
          created_by="owner", to=CHAT_OWNER)
    _seed(svc, id="sys", name="dream", kind="system_event")

    tool = _make_tool(
        svc, actor="owner", admins={"owner"},
        session_chat=CHAT_OWNER,
    )
    out = _run(tool, action="list")
    assert "member_a's note" in out
    assert "owner's note" in out
    assert "dream" in out
    assert "hidden" not in out


def test_legacy_jobs_without_created_by_visible_via_to_match(svc):
    """Pre-existing jobs (predating the field) must still be visible to
    their addressee. They have ``created_by=None`` but ``to=<chat>``."""
    _seed(svc, id="legacy1", name="old kid_fest reminder",
          created_by=None, to=SESSION_CHAT_MEMBER_A)

    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="list")
    assert "old kid_fest reminder" in out


def test_legacy_jobs_without_created_by_or_match_hidden(svc):
    """Job that doesn't address us and has no creator → only admin sees it."""
    _seed(svc, id="orphan", name="ancient orphan",
          created_by=None, to=CHAT_OWNER)
    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="list")
    assert "ancient orphan" not in out


def test_only_hidden_jobs_message(svc):
    _seed(svc, id="o1", name="someone else", created_by="owner",
          to=CHAT_OWNER)
    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="list")
    assert "No scheduled jobs visible to you" in out
    assert "1 hidden" in out


def test_no_jobs_at_all(svc):
    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="list")
    assert out == "No scheduled jobs."


# ---- standalone (no callbacks) preserves legacy behavior -------------------

def test_standalone_tool_lists_everything(svc):
    """nanobot-without-familia: no getter callbacks → list shows all jobs."""
    _seed(svc, id="a", name="A", created_by="alice", to="111")
    _seed(svc, id="b", name="B", created_by="bob", to="222")
    _seed(svc, id="sys", name="dream", kind="system_event")

    tool = CronTool(svc, default_timezone="UTC")
    tool.set_context("vk", "anything")
    out = _run(tool, action="list")
    assert "A" in out and "B" in out and "dream" in out


# ---- created_by stamping at add time --------------------------------------

def test_add_stamps_created_by(svc):
    tool = _make_tool(svc, actor="member_a", admins=set())
    _run(tool, action="add", message="ping",
         at="2030-01-01T00:00:00")
    [job] = svc.list_jobs()
    assert job.payload.created_by == "member_a"


def test_add_with_no_actor_getter_leaves_created_by_null(svc):
    tool = CronTool(svc, default_timezone="UTC")
    tool.set_context("vk", "x")
    _run(tool, action="add", message="ping", at="2030-01-01T00:00:00")
    [job] = svc.list_jobs()
    assert job.payload.created_by is None


# ---- remove ownership enforcement -----------------------------------------

def test_non_admin_cannot_remove_other_users_job(svc):
    _seed(svc, id="theirs", name="not yours",
          created_by="owner", to=CHAT_OWNER)

    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="remove", job_id="theirs")
    # 404-style response: don't leak job existence to non-owner.
    assert "not found" in out.lower()
    assert svc.get_job("theirs") is not None  # still there


def test_non_admin_can_remove_own_job(svc):
    _seed(svc, id="mine", name="my reminder",
          created_by="member_a", to=SESSION_CHAT_MEMBER_A)
    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="remove", job_id="mine")
    assert "Removed" in out
    assert svc.get_job("mine") is None


def test_non_admin_can_remove_job_addressed_to_them(svc):
    _seed(svc, id="forme", name="reminder for member_a",
          created_by="owner", to=SESSION_CHAT_MEMBER_A)
    tool = _make_tool(svc, actor="member_a", admins=set())
    out = _run(tool, action="remove", job_id="forme")
    # member_a is the recipient → she controls her own delivery.
    assert "Removed" in out


def test_add_with_unreachable_tag_rejected(svc):
    """SR-7: cron-side mirror of memory_set. Tag-write must be in reachable."""
    def reachable_only_self(actor: str | None) -> set[str]:
        return {actor} if actor else set()

    tool = CronTool(
        svc,
        default_timezone="UTC",
        current_actor_getter=lambda: "member_a",
        is_admin_getter=lambda a: False,
        reachable_tags_getter=reachable_only_self,
    )
    tool.set_context("vk", SESSION_CHAT_MEMBER_A)
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00",
               tags=["finance"])  # finance ∉ reachable for member_a
    assert "Error" in out
    assert "finance" in out
    # No job persisted.
    assert svc.list_jobs() == []


def test_add_with_reachable_tags_persists_with_tags_field(svc):
    def reachable(actor: str | None) -> set[str]:
        return {"varya", "school"} if actor == "member_a" else set()

    tool = CronTool(
        svc,
        default_timezone="UTC",
        current_actor_getter=lambda: "member_a",
        is_admin_getter=lambda a: False,
        reachable_tags_getter=reachable,
    )
    tool.set_context("vk", SESSION_CHAT_MEMBER_A)
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00",
               tags=["varya", "school"])
    assert "Created job" in out
    [job] = svc.list_jobs()
    assert sorted(job.payload.tags) == ["school", "varya"]


def test_list_filters_by_tag_intersection(svc):
    """A job tagged ``varya`` is visible to anyone whose reachable set
    contains varya, even if they didn't create it and aren't the recipient."""
    _seed(svc, id="t1", name="varya-tagged",
          created_by="member_a", to=SESSION_CHAT_MEMBER_A,
          # tags injected via direct service path
          )
    # Inject tags into the seeded job (bypass: the seed helper doesn't accept tags).
    job = svc.get_job("t1")
    job.payload.tags = ["varya"]
    svc._save_store()

    def reachable_owner(actor: str | None) -> set[str]:
        return {"varya", "owner"} if actor == "owner" else set()

    tool = CronTool(
        svc,
        default_timezone="UTC",
        current_actor_getter=lambda: "owner",
        is_admin_getter=lambda a: False,  # not admin to force tag-path
        reachable_tags_getter=reachable_owner,
    )
    tool.set_context("vk", CHAT_OWNER)
    out = _run(tool, action="list")
    assert "varya-tagged" in out


def test_admin_can_remove_anyone_job(svc):
    _seed(svc, id="anyones", name="random",
          created_by="member_a", to=SESSION_CHAT_MEMBER_A)
    tool = _make_tool(
        svc, actor="owner", admins={"owner"},
        session_chat=CHAT_OWNER,
    )
    out = _run(tool, action="remove", job_id="anyones")
    assert "Removed" in out
