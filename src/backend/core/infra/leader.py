"""Work that must happen in exactly one backend process.

The backend serves requests from several uvicorn workers, but a large part of
what it starts at boot is not per-worker work: reapers, schedulers, recovery
passes, outbound channel connections and the seeding of default rows. Running
those once per worker is not merely wasteful — a scheduler tick fires a task N
times, a recovery pass fights itself over the same orphan rows, and two seeders
race on the same unique key.

Two shapes cover every such step, and both are expressed against the
``hold``/``claim`` mutex in :mod:`core.infra.ephemeral`, so a deployment
without Redis (the desktop's single-process backend) gets the same semantics
from the in-process store it already uses for every other short-lived key.

``Leadership``
    A renewed lease for the long-lived loops. One worker wins it and starts
    them; it keeps the lease alive for as long as it is healthy. If that worker
    dies, the lease lapses and the next worker to look takes over and starts
    them there — which is the property a bare "decide once at boot" check does
    not have, and the reason this is a loop rather than a single check.

``run_once``
    A barrier for the one-shot boot steps. Exactly one worker runs the step;
    the others wait for it to finish before continuing, so no worker begins
    serving against half-seeded state.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from typing import Awaitable, Callable, Optional

from core.infra.ephemeral import get_ephemeral_state
from core.infra.logging import get_logger
from core.infra.worker_count import resolve as resolved_workers

logger = get_logger(__name__)

_LEADER_PREFIX = "jx:leader:"
_ONCE_PREFIX = "jx:boot:"

# The lease has to outlive a renewal that is merely slow — a worker pausing for
# a GC or a busy disk must not hand its loops to a second process — while still
# expiring soon enough that a *dead* worker's loops restart promptly. Renewal
# runs at a third of it, so two consecutive renewals can be lost before the
# lease actually lapses.
_LEASE_SECONDS = 45
_RENEW_SECONDS = _LEASE_SECONDS / 3

# How often a worker waiting on a boot barrier looks to see whether the worker
# running the step has finished.
_POLL_SECONDS = 0.2


def _token() -> str:
    """An identity no other process can accidentally present."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def coordination_required() -> bool:
    """Whether this deployment actually runs more than one backend process.

    A single-process backend has nobody to coordinate with, and saying so is
    not an optimisation — it is the difference between booting and not: the
    desktop profile runs one process with no Redis at all, and neither it nor a
    deployment whose Redis is briefly down should acquire a dependency on a
    mutex that can only ever be uncontended.

    It asks :func:`~core.infra.worker_count.resolve` rather than reading
    ``WEB_CONCURRENCY``, and that distinction is load-bearing twice over. The
    resolved count already refuses multi-worker without Redis, which is exactly
    when :func:`get_ephemeral_state` would hand back the per-process store and
    this lease would quietly stop being a lease. And a launch path that never
    ran the entrypoint — ``cli.py``, ``api.app.main`` — would otherwise read the
    *requested* value and believe a single process is a cluster.
    """
    return resolved_workers() > 1


class Leadership:
    """A renewed lease naming one process the owner of a role's singleton work."""

    def __init__(self, role: str, *, lease_seconds: int = _LEASE_SECONDS) -> None:
        self._key = f"{_LEADER_PREFIX}{role}"
        self._role = role
        self._token = _token()
        self._lease = int(lease_seconds)
        self._elected = False
        self._task: Optional[asyncio.Task] = None
        self._work: Optional[asyncio.Task] = None

    async def _hold(self) -> bool:
        if not coordination_required():
            return True
        try:
            return await get_ephemeral_state().hold(self._key, self._token, ttl=self._lease)
        except Exception as exc:  # noqa: BLE001 - a store blip must not depose a healthy leader
            logger.warning("leader_hold_failed", role=self._role, error=str(exc))
            return self._elected

    async def _supervise(
        self,
        on_elected: Callable[[], Awaitable[None]],
        on_deposed: Callable[[], Awaitable[None]],
    ) -> None:
        while True:
            holding = await self._hold()
            if holding and not self._elected:
                self._elected = True
                logger.info("leader_elected", role=self._role, token=self._token)
                # Started, never awaited: the callback brings up every singleton
                # loop in turn and can easily outlast a lease. Awaiting it here
                # would stop renewing for that whole stretch, the lease would
                # lapse mid-startup, and a second worker would elect itself and
                # bring up a duplicate set — the exact outcome this exists to
                # prevent, now paid for twice.
                self._work = asyncio.create_task(on_elected(), name=f"leader_work:{self._role}")
                if not coordination_required():
                    # Uncontended by construction: nothing can take this role,
                    # so there is nothing left to supervise. Returning keeps
                    # single-process deployments — the default, and the
                    # battery-powered desktop one — free of a forever timer.
                    return
            elif not holding and self._elected:
                # Losing a lease we were renewing means this process stalled
                # past the whole lease; another worker owns the loops now and
                # ours have to stop, or both run.
                self._elected = False
                logger.warning("leader_deposed", role=self._role, token=self._token)
                await self._cancel_work()
                await on_deposed()
            await asyncio.sleep(_RENEW_SECONDS)

    async def _cancel_work(self) -> None:
        if self._work is None or self._work.done():
            return
        self._work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._work

    def start(
        self,
        *,
        on_elected: Callable[[], Awaitable[None]],
        on_deposed: Callable[[], Awaitable[None]],
    ) -> None:
        self._task = asyncio.create_task(
            self._supervise(on_elected, on_deposed), name=f"leader:{self._role}"
        )

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        # The elected work used to live in the deferred-startup task, which the
        # shutdown path cancels; now it is ours, so cancelling it is too.
        await self._cancel_work()
        if self._elected:
            self._elected = False
            try:
                await get_ephemeral_state().drop(self._key)
            except Exception as exc:  # noqa: BLE001 - shutdown must not raise
                logger.warning("leader_release_failed", role=self._role, error=str(exc))


async def run_once(
    name: str, step: Callable[[], Awaitable[None]], *, timeout: float = 300.0
) -> None:
    """Run *step* in one worker of this boot; the rest wait for it to finish.

    The workers of a single boot share a parent — uvicorn's master — so its pid
    names this boot without any coordination between them. The done marker is
    deliberately short-lived: a later restart is *supposed* to run the boot
    steps again, and only workers of the same boot, which all start within a
    few seconds of one another, ever need to see it.
    """
    if not coordination_required():
        await step()
        return

    state = get_ephemeral_state()
    boot = f"{_ONCE_PREFIX}{socket.gethostname()}:{os.getppid()}:{name}"
    lock, done = f"{boot}:lock", f"{boot}:done"

    if await state.claim(lock, ttl=int(timeout)):
        try:
            await step()
        finally:
            # The marker is written even when the step raised: a failed boot
            # step is this worker's problem to log, not a reason to hang every
            # other worker until the timeout.
            await state.put(done, "1", ttl=60)
        return

    for _ in range(int(timeout / _POLL_SECONDS)):
        if await state.get(done) is not None:
            return
        await asyncio.sleep(_POLL_SECONDS)
    logger.warning("boot_step_wait_timeout", name=name, after_seconds=timeout)


__all__ = ["Leadership", "coordination_required", "run_once"]
