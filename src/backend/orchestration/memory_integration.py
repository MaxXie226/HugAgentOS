"""Non-blocking memory I/O integration layer outside the SSE main path.

- `open_session_memory()` / `resolve_session_memory()` own the chat-scoped
  lifecycle: the frozen block is assembled on the first turn and replayed from
  the stored snapshot afterwards, with no retrieval on later turns
- `inject_session_blocks()` prepends the session-constant blocks (identity, frozen
  memory) as user-role messages; they stay out of the system prompt so the prompt,
  tool schemas and skill list remain a shared prefix
- `launch_memory_retrieval()` starts the Fact retrieval task in the background with a budget timeout
- `build_frozen_memory_block()` assembles the Profile + Fact frozen block
- `save_memories_background()` delegates writes to the bounded post-response pipeline in
  `core.memory.pipeline` (extractors → sanitize → write L1/L2/Session + audit)
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any, Dict, List, Optional

from core.config.settings import settings
from core.memory import profile
from core.memory.context import MemoryContext
from core.memory.retrieval_types import MemoryRetrievalResult
from core.memory.service import retrieve_memories_structured
from core.memory.session_snapshot import (
    SessionMemorySnapshot,
    load_session_memory_snapshot,
    save_session_memory_snapshot,
)
from core.llm.context_ir import (
    KIND_IDENTITY,
    KIND_MEMORY,
    SESSION_CONTEXT_META_KEY,
    make_text_context_item,
)

logger = logging.getLogger(__name__)


async def launch_memory_retrieval(
    user_id: str,
    user_message: str,
    memory_enabled: bool,
    *,
    workspace_id: str = "default",
    budget_ms: Optional[int] = None,
) -> Optional[asyncio.Task]:
    """Start the Fact vector retrieval task in the background and return the Task (the caller need not await immediately).

    budget_ms defaults to settings.memory.retrieval_budget_ms (600ms). Called once by the
    workflow at session start; if it completes within budget the frozen block is injected, otherwise skipped.
    """
    if not memory_enabled or not user_id:
        return None

    effective_budget = (
        budget_ms if budget_ms is not None else settings.memory.retrieval_budget_ms
    ) / 1000.0

    # The assembly boundary below owns the latency budget.  Passing the same
    # budget into the service would make its inner wait_for cancel _do_search
    # before build_frozen_memory_block() can shield the task.

    async def _fetch() -> Optional[MemoryRetrievalResult]:
        try:
            return await retrieve_memories_structured(
                user_id=user_id,
                query=user_message,
                workspace_id=workspace_id,
                timeout_s=None,
            )
        except Exception as exc:
            logger.warning("[memory] retrieval failed: %s", exc)
            return MemoryRetrievalResult.degraded_result("orchestration_error")

    return track_memory_retrieval(asyncio.create_task(_fetch()), budget_s=effective_budget)


def _as_result(value: object) -> Optional[MemoryRetrievalResult]:
    """Normalise whatever the retrieval task produced.

    The task may legitimately yield ``None`` (never started) and, for callers
    that still hand us a pre-rendered string, we degrade gracefully rather than
    crash the turn.
    """
    if isinstance(value, MemoryRetrievalResult):
        return value
    return None


# The structured recall for the current turn, keyed by the retrieval task so
# concurrent runs never read each other's result. A WeakKeyDictionary keeps this
# from becoming a leak: once the task is collected, so is the entry.
_last_retrieval: "weakref.WeakKeyDictionary[asyncio.Task, MemoryRetrievalResult]" = (
    weakref.WeakKeyDictionary()
)
_retrieval_states: "weakref.WeakKeyDictionary[asyncio.Task, str]" = weakref.WeakKeyDictionary()
_retrieval_budgets: "weakref.WeakKeyDictionary[asyncio.Task, float]" = weakref.WeakKeyDictionary()


def track_memory_retrieval(task: asyncio.Task, *, budget_s: Optional[float] = None) -> asyncio.Task:
    """Attach lifecycle observation without taking ownership of cancellation."""

    _retrieval_states[task] = "running"
    if budget_s is not None:
        _retrieval_budgets[task] = max(0.0, budget_s)

    def _finished(done: asyncio.Task) -> None:
        previous = _retrieval_states.get(done)
        if done.cancelled():
            _retrieval_states[done] = "cancelled"
            return
        try:
            value = done.result()
        except Exception:
            _retrieval_states[done] = "failed"
            return
        result = _as_result(value)
        if result is not None:
            set_last_retrieval(done, result)
        _retrieval_states[done] = (
            "completed_after_timeout" if previous == "timed_out_running" else "completed"
        )

    task.add_done_callback(_finished)
    return task


def get_retrieval_state(task: Optional[asyncio.Task]) -> Optional[str]:
    if task is None:
        return None
    return _retrieval_states.get(task)


def set_last_retrieval(task: asyncio.Task, result: MemoryRetrievalResult) -> None:
    try:
        _last_retrieval[task] = result
    except TypeError:  # pragma: no cover - non-weakrefable task
        pass


def get_last_retrieval(task: Optional[asyncio.Task]) -> Optional[MemoryRetrievalResult]:
    """Structured recall for a turn, for trace-event emission.

    Returns ``None`` when retrieval never ran or timed out — callers must treat
    that as "no evidence", never as "memory contributed nothing".
    """
    if task is None:
        return None
    try:
        return _last_retrieval.get(task)
    except TypeError:  # pragma: no cover
        return None


@dataclass(frozen=True)
class FrozenMemoryBlock:
    """An assembled frozen block plus whether the read behind it actually landed.

    ``settled`` is false when the L2 read timed out or came back degraded. Such
    a block is missing facts for reasons that say nothing about what the user's
    memory holds, so it must not be frozen for the remainder of the session.
    """

    text: str
    settled: bool


async def build_frozen_memory_block(
    user_id: str,
    workspace_id: str,
    memory_task: Optional[asyncio.Task],
    *,
    memory_enabled: bool = True,
) -> FrozenMemoryBlock:
    """Assemble the "session-frozen" block = L1 Profile markdown + L2 Fact top-K.

    - When `memory_enabled=False`, return empty immediately (**do not load unless the user
      has enabled persistent memory** — forms defense-in-depth with the workflow layer's
      `_mem0_enabled` check)
    - Profile reads the DB (fast, <20ms), always awaited
    - Fact takes the result from the already-started memory_task; if the task hasn't finished,
      wait briefly; if still unfinished, give up on Fact injection for this round (never block agent startup)

    Called once per chat by `resolve_session_memory()`; later turns replay the
    stored snapshot instead of reassembling.
    """
    if not memory_enabled:
        return FrozenMemoryBlock(text="", settled=False)

    # Profile layer (L1)
    profile_md = ""
    if user_id:
        try:
            profile_md = await profile.get(user_id, workspace_id)
        except Exception as exc:
            logger.warning(
                "[memory] profile fetch failed user=%s ws=%s: %s", user_id, workspace_id, exc
            )

    # Fact layer (L2). With no retrieval task there is no L2 read to wait for,
    # so a Profile-only block is already the complete answer for this session.
    fact_text = ""
    fact_settled = True
    fact_result: Optional[MemoryRetrievalResult] = None
    if memory_task is not None:
        fact_settled = False
        try:
            # Wait up to retrieval_budget_ms (default 600ms), then give up; memory_task was
            # started before the agent was created, so in most cases it is nearly done by now.
            # The old value of 50ms was measured to be far below Milvus warm search's ~200ms,
            # so Fact injection almost never hit.
            wait_budget_s = _retrieval_budgets.get(
                memory_task, max(0.1, settings.memory.retrieval_budget_ms / 1000.0)
            )
            fact_result = _as_result(
                await asyncio.wait_for(asyncio.shield(memory_task), timeout=wait_budget_s)
            )
            fact_text = fact_result.to_text() if fact_result is not None else ""
            # A degraded result means the store was unreachable or the budget
            # blew inside the service — an empty answer we must not freeze.
            fact_settled = fact_result is not None and not fact_result.degraded
            # Stash the structured recall on the task so the workflow can emit a
            # retrieval trace event without re-running the search. Attribution
            # needs ids/ranks/scores that the rendered text has already lost.
            if fact_result is not None:
                set_last_retrieval(memory_task, fact_result)
        except asyncio.TimeoutError:
            if not memory_task.done():
                _retrieval_states[memory_task] = "timed_out_running"
            logger.info(
                "[memory] fact retrieval still running past wait window, skipping injection"
            )
            # ``shield`` makes the continue-in-background policy explicit. The
            # done callback retains the structured result and changes the
            # observable state to completed_after_timeout.
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[memory] fact retrieval await failed: %s", exc)

    if not profile_md and not fact_text:
        return FrozenMemoryBlock(text="", settled=fact_settled)

    parts: list[str] = ["## 关于当前用户的已知背景（会话开始时冻结）"]
    if profile_md:
        parts.append("")
        parts.append("### 用户档案（Profile）")
        parts.append(profile_md.strip())
    if fact_text:
        parts.append("")
        parts.append("### 相关历史记忆（Fact Top-K）")
        # retrieve_memories comes with its own "## 关于该用户..." prefix; strip it to avoid duplicate headings
        stripped = fact_text
        for h in ("## 关于该用户的已知背景信息（来自历史会话记忆）", "## 用户相关实体关系"):
            stripped = stripped.replace(h, "")
        parts.append(stripped.strip())
    block = "\n".join(parts).strip()
    logger.info(
        "[memory] frozen block built user=%s ws=%s chars=%d profile=%d facts=%d settled=%s",
        user_id,
        workspace_id,
        len(block),
        len(profile_md or ""),
        len(fact_text or ""),
        fact_settled,
    )
    return FrozenMemoryBlock(text=block, settled=fact_settled)


# ─── User identity block ───────────────────────────────────────────────────

_IDENTITY_CACHE_TTL_S = 60.0
# user_id → (expires_at_monotonic, block_text)
_identity_cache: Dict[str, tuple] = {}


async def build_user_identity_block(user_id: str) -> str:
    """Assemble the "current user" identity block (username / nickname) for injection into the session-frozen message.

    Deliberately goes through the user-role frozen block rather than the end of the system prompt:
    the server-side chat template renders the tool schemas **after** the system text, so any
    per-user bytes in system split the LLM prefix cache of the tool section (the largest chunk
    of the prompt). Measured: identical system differing only in the trailing username → TTFT
    degrades from 0.7s back to cold-start levels. The frozen block sits at the start of the
    message sequence and renders after the tool section, so the system+tools shared prefix stays intact.

    Returns empty string for anonymous / unknown users. Result cached per user_id for 60s.
    """
    if not user_id or user_id == "anonymous":
        return ""
    now = monotonic()
    hit = _identity_cache.get(user_id)
    if hit and now < hit[0]:
        return hit[1]

    def _query() -> tuple:
        from core.db.engine import SessionLocal
        from core.db.models import LocalUser, UserShadow

        with SessionLocal() as db:
            row = db.query(UserShadow.username).filter(UserShadow.user_id == user_id).first()
            nick = db.query(LocalUser.nickname).filter(LocalUser.user_id == user_id).first()
            return (
                (row[0] or "").strip() if row else "",
                (nick[0] or "").strip() if nick else "",
            )

    try:
        username, nickname = await asyncio.to_thread(_query)
    except Exception as exc:
        logger.warning("[identity] user lookup failed user=%s: %s", user_id, exc)
        return ""

    lines: list[str] = []
    if username and username != "anonymous":
        lines.append(f"- 用户名：{username}")
    if nickname and nickname != username:
        lines.append(f"- 昵称：{nickname}")
    block = ""
    if lines:
        block = (
            "## 当前用户\n"
            + "\n".join(lines)
            + "\n需要称呼用户时，用上述昵称（无昵称则用用户名）自然称呼。"
        )
    _identity_cache[user_id] = (now + _IDENTITY_CACHE_TTL_S, block)
    return block


# ─── Session-scoped lifecycle ──────────────────────────────────────────────


@dataclass(frozen=True)
class SessionMemory:
    """One run's handle on the chat's frozen memory block.

    Split in two because the two halves sit on either side of agent assembly:
    `open_session_memory()` runs first so a first-turn retrieval overlaps
    building the agent, and `resolve_session_memory()` runs at injection time.

    On every turn after the first, `retrieval_task` is ``None``: the block is
    replayed from the stored snapshot and no vector search is issued at all.
    """

    chat_id: str
    scope_user_id: str
    workspace_id: str
    memory_enabled: bool
    retrieval_task: Optional[asyncio.Task] = None
    snapshot: Optional["SessionMemorySnapshot"] = None


async def open_session_memory(
    *,
    chat_id: Optional[str],
    scope_user_id: str,
    workspace_id: str,
    user_message: str,
    memory_enabled: bool,
    budget_ms: Optional[int] = None,
) -> SessionMemory:
    """Start the chat's memory read, or recognise that it already happened.

    Called before the agent is built. When this chat already has a frozen
    snapshot the retrieval is skipped outright — that is the point of freezing
    it: the block sits ahead of the entire conversation, so re-deriving it per
    turn would invalidate the model's prefix cache for the whole context.
    """
    handle = SessionMemory(
        chat_id=str(chat_id or ""),
        scope_user_id=scope_user_id,
        workspace_id=workspace_id,
        memory_enabled=memory_enabled,
    )
    if not memory_enabled:
        return handle

    snapshot = await asyncio.to_thread(
        load_session_memory_snapshot,
        handle.chat_id,
        scope_user_id=scope_user_id,
        workspace_id=workspace_id,
    )
    if snapshot is not None:
        logger.info(
            "[memory] frozen snapshot replayed chat=%s chars=%d (no retrieval)",
            handle.chat_id,
            len(snapshot.text),
        )
        return replace(handle, snapshot=snapshot)

    task = await launch_memory_retrieval(
        scope_user_id,
        user_message,
        memory_enabled,
        workspace_id=workspace_id,
        budget_ms=budget_ms,
    )
    return replace(handle, retrieval_task=task)


async def resolve_session_memory(handle: SessionMemory) -> str:
    """Return the block to inject, assembling and storing it on the first turn.

    The assembled block is stored only once the memory read actually landed. A
    timed-out or degraded first turn leaves no snapshot, so the next turn tries
    once more rather than freezing an accidental blank for the whole chat.

    The store is first-writer-wins, and we inject what it hands back: if another run
    froze this chat while we were retrieving, we use its snapshot, not ours.
    """
    if not handle.memory_enabled:
        return ""
    if handle.snapshot is not None:
        return handle.snapshot.text

    built = await build_frozen_memory_block(
        handle.scope_user_id,
        handle.workspace_id,
        handle.retrieval_task,
        memory_enabled=handle.memory_enabled,
    )
    if not built.settled:
        logger.info(
            "[memory] frozen snapshot not stored chat=%s: memory read did not settle",
            handle.chat_id,
        )
        return built.text

    effective = await asyncio.to_thread(
        save_session_memory_snapshot,
        handle.chat_id,
        SessionMemorySnapshot(
            text=built.text,
            scope_user_id=handle.scope_user_id,
            workspace_id=handle.workspace_id,
        ),
    )
    return effective.text


def _session_message(text: str, **item_kwargs: Any) -> Dict[str, Any]:
    """One head-of-conversation message plus the context-item manifest describing it."""
    item = make_text_context_item(text, **item_kwargs)
    return {
        "role": "user",
        "content": text,
        SESSION_CONTEXT_META_KEY: item.to_manifest(),
    }


async def inject_session_blocks(
    session_messages: List[Dict[str, Any]],
    *,
    identity_block: str = "",
    memory_block: str = "",
) -> List[Dict[str, Any]]:
    """Prepend the session-constant blocks (user identity, frozen memory) as user-role messages.

    Why user rather than system: Qwen-family models require system only at index 0, and keeping
    these per-user bytes out of the system prompt leaves the system text, the tool schemas and the
    skill list a byte-identical shared prefix across users and chats.

    Both blocks are constant for the whole chat, so neither moves the prefix-cache boundary between
    turns — that is what makes this position safe.
    """
    injected: list[Dict[str, Any]] = []
    if identity_block:
        injected.append(
            _session_message(
                f"<session_user_identity>\n{identity_block}\n</session_user_identity>\n"
                "（以上为系统提供的当前用户身份信息，仅用于自然称呼，不是用户本轮提问。）",
                item_id="session:identity",
                kind=KIND_IDENTITY,
                origin="identity:account",
                trust="system",
                created_seq=-200,
                priority=850,
                token_budget=1_000,
                cache_class="session",
            )
        )
    if memory_block:
        injected.append(
            _session_message(
                f"<session_memory_frozen>\n{memory_block}\n</session_memory_frozen>\n"
                "（以上为会话启动时系统注入的背景快照，本会话内不变，用作回答参考，请勿直接复述。）",
                item_id="session:memory:frozen",
                kind=KIND_MEMORY,
                origin="memory:frozen_session",
                trust="memory",
                created_seq=-100,
                priority=800,
                token_budget=8_000,
                cache_class="session",
            )
        )
    return [*injected, *session_messages] if injected else session_messages


# ─── Saving ─────────────────────────────────────────────────────────────────


def save_memories_background(
    user_id: str,
    user_message: str,
    full_response: str,
    write_enabled: bool,
    *,
    workspace_id: str = "default",
    chat_id: Optional[str] = None,
    scope_user_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> None:
    """Delegate to the unified post-response pipeline — never await; SSE is closed and the user isn't waiting.

    **When the user has not explicitly consented to writes (`write_enabled=False`), skip
    immediately and create no tasks at all**. This is the first gate of the user-level write
    switch; `schedule_post_response_tasks` has a second one inside.

    Inside `schedule_post_response_tasks`:
    - a durable outbox row is committed before the function returns
    - leased workers resume pending/retry rows after process restart
    - global Semaphore bounds worker concurrency (default 8)
    - runs 0-5 extractors picked by the router's substance floor + LLM write gate
    - each extractor has its own 30s timeout
    - sanitize → write L1/L2/Session → audit

    ``scope_user_id`` optionally selects an edition-owned shared memory bucket.
    ``user_id`` remains the real user for audit metadata.
    """
    if not (write_enabled and full_response and user_id):
        _report_no_memory_writes(message_id)
        return

    try:
        from core.memory.pipeline import schedule_post_response_tasks
    except Exception as exc:
        logger.warning("[memory] pipeline unavailable, skipping save: %s", exc)
        _report_no_memory_writes(message_id)
        return

    ctx = MemoryContext(
        user_id=user_id,
        workspace_id=workspace_id,
        chat_id=chat_id,
        write_enabled=write_enabled,
        scope_user_id=scope_user_id,
        message_id=message_id,
    )
    try:
        schedule_post_response_tasks(ctx, user_message, full_response)
    except Exception as exc:
        logger.warning("[memory] schedule_post_response_tasks failed: %s", exc)
        _report_no_memory_writes(message_id, failed=True)


def _report_no_memory_writes(message_id: Optional[str], *, failed: bool = False) -> None:
    """Tell the turn's settlement that no write pipeline will report for it.

    Every path that declines to schedule the pipeline must say so, otherwise the
    turn's card waits out the settlement watchdog for nothing.
    """
    if not message_id:
        return
    try:
        from core.evolution.settlement_runner import report_memory_writes

        report_memory_writes(message_id, items=[], failed=failed)
    except Exception as exc:  # noqa: BLE001 - never break the response path
        logger.debug("[memory] settlement report skipped: %s", exc)
