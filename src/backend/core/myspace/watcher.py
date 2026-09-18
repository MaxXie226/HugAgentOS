"""「我的空间」的登记由文件系统驱动，不由某个工具顺手完成。

原来的做法是「谁改了 ``/myspace``，就由谁把改动登记回账本」：write / edit 工具各自调
``sync_upsert``，文件增删改工具调 ``sync_delete`` / ``sync_move``，bash 在命令前后拍目录
快照做差集，界面打开时再补一次账。这套写法的前提是「改动只可能从这几个口子进来」，而这个
前提不成立：nohup 起的后台进程在命令返回之后才写文件，子智能体和批量任务在另一条协程里
写，技能里的 CLI 直接写，MCP 服务端也写。每多一个入口就要多补一处登记，漏掉的那一处的
表现是——文件确实躺在用户网盘的磁盘上，界面上却看不见、也删不掉，而每个新建沙箱都会把
这份目录挂进来，于是成了「上个会话的残留中间文件」。生产上实测某个账号顶层 143 个文件里
有 133 个处在这个状态。

所以判据换成文件系统自己：``myspace_cache/`` 下发生任何写入或删除，都由本模块登记回账本。
谁写的、经不经过工具，都一样，也就不存在"又漏了一个入口"。

几个要点：

**历史欠账不在这里补。** 改造上线之前积压的未登记文件是一次性的数据迁移，用
``scripts/reconcile_myspace_mirror.py`` 跑一次就完事，不做成每次启动都执行的扫描——那既是
白跑，也会掩盖实时这条路上真正的漏洞。

**每个 worker 都监听，认领去重。** 确认条的 pending / Event / 前端信号队列都活在进程里
（见 ``core.llm.tools._myspace_confirm``），只有正在跑这个会话的那个进程弹得出来。所以
不能只让 leader 监听，而是每个 worker 都监听同一份目录，再用
:mod:`core.infra.ephemeral` 的认领保证一次改动只被处理一次。本进程没有这个用户的活跃
会话时先让一步（:data:`_HANDOFF_GRACE_S`），把机会留给弹得出确认条的那个进程。

**登记不等于交付。** 本模块只登记，不挂会话、也不往任何会话的产物区里挂文件卡片。
「我的空间」是用户级的一份目录，每个会话的沙箱都挂着它，文件事件里只有
``myspace_cache/{uid}/...``，没有会话身份 —— 是谁写的，这里根本无从得知。曾经拿"这个用户
当下随便哪个在跑的会话"顶上，结果是另一个会话写的文件被记到了这个会话名下，还在它的产物
区里多出一张卡片。卡片只能由**写文件的那一轮自己**挂（``core.llm.workspace`` 的 ContextVar
就是"当前这一轮"），这里不在任何一轮里。

**确认门变成"退回去"。** 文件在磁盘上早就改了/删了，确认能做的不是拦住，而是在用户否决
之后从对象存储把那一版还原回来。问不到人时（没有活跃会话、子智能体、批量、定时任务）直接
生效——此时拒绝拦不住已经发生的事，只会让两边不一致。

**不需要"这是我自己写的"登记表。** 后端自己往镜像目录写文件（界面上传的物化、还原）时把
mtime 对齐到账本时间（``mirror.stamp_registered``），于是"磁盘比账本新"始终只意味着"沙箱
写的、还没登记"，监听器不会把后端自己的写入当成新改动转头再登记一次。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from core.llm.tools._myspace_confirm import OP_DELETE, OP_EDIT
from core.llm.tools.myspace_vfs import MYSPACE_LOGICAL
from core.myspace import mirror
from core.sandbox._common import myspace_cache_root
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

# 一次写入会连着发很多个 modify 事件（解压、大文件、边写边刷），安静这么久才算写完。
_SETTLE_S = 1.5
# 去抖循环自己出错时退避多久再来 —— 正常路径不轮询，它挂在事件上等。
_ERROR_BACKOFF_S = 0.5
# 同时在办的登记最多占多少内存。登记要把文件整个读进来再交给对象存储（新建那条路还会
# 在内部 base64 一轮），所以限流的单位得是**字节**而不是"几个文件"：按个数放 8 个并发，
# 碰上 8 个上限大小的文件就是近 1GB 的峰值。
_INFLIGHT_BUDGET_BYTES = 256 * 1024 * 1024
# 每个登记至少按这么多算，于是小文件的并发度 = 额度 / 这个值（默认 8 个），和按个数限流
# 时一样；大文件按实际大小占额度，自然排队。
_MIN_COST_BYTES = 32 * 1024 * 1024
# 一份内容在内存里同时存在几份（原始字节 + 入库路径上的编码副本）。
_MEMORY_FACTOR = 3
# 本进程没有这个用户的活跃会话时先等一会儿，把机会让给托管着会话的那个进程。
_HANDOFF_GRACE_S = 4.0
# 认领的有效期：够一次登记（读盘 + 上传 + DB）跑完，又不会长到把同一路径的下次改动挡住。
_CLAIM_TTL_S = 300
# 确认条等用户多久。比工具里的默认短 —— 这里没有模型悬在半空等着，挂太久只是占并发额度。
_CONFIRM_TIMEOUT_S = 300.0


def _split(raw: str, root: Path) -> Optional[tuple[str, str]]:
    """把文件系统路径拆成 ``(user_id, 相对用户根目录的路径)``；不该管的返回 ``None``。"""
    try:
        rel = Path(raw).relative_to(root)
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) < 2:
        return None  # 用户目录本身，不是用户的文件
    if any(p in mirror.SKIP_DIR_NAMES for p in parts[1:]):
        return None
    return parts[0], "/".join(parts[1:])


async def _claim(user_id: str, rel: str, stamp: str) -> bool:
    """跨进程认领这一次改动 —— 每个 worker 都看得见同一个事件，只该有一个去登记。

    ``stamp`` 让同一路径的每次改动各自认领：写入用 mtime，删除用被删记录的 id。
    """
    from core.infra.ephemeral import get_ephemeral_state

    digest = hashlib.sha1(f"{user_id}/{rel}".encode()).hexdigest()[:16]
    try:
        return await get_ephemeral_state().claim(
            f"jx:myspace:reg:{user_id}:{digest}:{stamp}", ttl=_CLAIM_TTL_S
        )
    except Exception as exc:  # noqa: BLE001 — 认领不上就当没抢到，宁可少做也不重复做
        logger.warning("[myspace-registry] 认领失败 %s/%s: %s", user_id, rel, exc)
        return False


def _confirm_chat(user_id: str) -> Optional[str]:
    """确认条弹到哪个会话里去；没处可弹则 ``None``。

    **只决定"问谁"，不是文件的归属**：同时跑着几个会话时这里返回的未必是写文件的那个。

    这是围绕一个已知缺口搭的脚手架：确认条的 pending / Event / 前端信号队列都活在进程里
    （见 ``core/llm/tools/_myspace_confirm.py`` 开头那段"多 worker 需要 Redis + pub/sub"），
    所以"谁来问"只能按进程判。等那个门做成跨进程的，本函数连同 :func:`_claim`、
    :data:`_HANDOFF_GRACE_S` 和"每个 worker 各挂一个监听器"一起都可以去掉。
    """
    try:
        from orchestration import chat_run_executor as cre

        if not cre.has_local_runs():  # 便宜的前置判断，省掉一次 DB 查询
            return None
        for run in cre.list_active_runs_for_user(user_id):
            if cre.is_local_run(str(run.run_id)):
                return str(run.chat_id)
    except Exception as exc:  # noqa: BLE001 — 查不到就按"问不到人"处理
        logger.warning("[myspace-registry] 查活跃会话失败 user=%s: %s", user_id, exc)
    return None


def _stat_all(user_id: str, rels: list[str]) -> dict[str, Optional[mirror.MirrorEntry]]:
    """一批路径现在还在不在磁盘上（只 stat，不查库）。认领要用它给出的 mtime。"""
    return {rel: mirror.mirror_entry(user_id, rel) for rel in rels}


class _Budget:
    """同时在办的登记占多少内存，按字节限流。

    单个文件再大也不会永远等不到：没有别人在办时，超额的那一个直接放行——排队是为了不让
    峰值叠加，不是为了拦住谁。
    """

    def __init__(self, total: int) -> None:
        self._total = total
        self._used = 0
        self._cv = asyncio.Condition()

    def cost(self, size: Optional[int]) -> int:
        want = max(int(size or 0) * _MEMORY_FACTOR, _MIN_COST_BYTES)
        return min(want, self._total)

    @asynccontextmanager
    async def reserve(self, size: Optional[int]) -> AsyncIterator[None]:
        cost = self.cost(size)
        async with self._cv:
            while self._used + cost > self._total and self._used > 0:
                await self._cv.wait()
            self._used += cost
        try:
            yield
        finally:
            async with self._cv:
                self._used -= cost
                self._cv.notify_all()


class _Ask:
    """确认条问谁 —— 查一次、缓存一次。

    绝大多数批次全是新文件（模型刚写出来的），谁也不用问；而 :func:`_confirm_chat` 要开
    一次 DB 会话把这个用户所有在跑的 run 拉出来。催办（``list_myspace_files`` / 打开
    「我的空间」）走的正是这条路，所以不能在批次入口无条件查。
    """

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id
        self._chat: Optional[str] = None
        self._asked = False

    async def get(self) -> Optional[str]:
        if not self._asked:
            self._chat = await asyncio.to_thread(_confirm_chat, self._user_id)
            self._asked = True
        return self._chat


class _Handler(FileSystemEventHandler):
    """watchdog 在自己的线程里回调，这里只负责把路径丢回事件循环。"""

    def __init__(self, owner: "MySpaceRegistry") -> None:
        self._owner = owner

    def on_any_event(self, event) -> None:  # noqa: ANN001 — watchdog 的事件类型
        self._owner._on_event(event)


class MySpaceRegistry:
    """监听镜像目录，把每一次写入和删除登记回「我的空间」。"""

    def __init__(self) -> None:
        self._root: Optional[Path] = None
        self._observer: Optional[Observer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict[tuple[str, str], float] = {}
        self._inflight: set[tuple[str, str]] = set()
        self._budget: Optional[_Budget] = None
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._drain: Optional[asyncio.Task] = None
        self._closed = False

    # ───── 生命周期 ────────────────────────────────────────────────────────

    async def start(self) -> None:
        root = myspace_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        # 不做 resolve：watchdog 回调里的路径是拿 schedule() 收到的那个字符串拼出来的，
        # 存储根是软链时两边一解一不解就对不上，事件会被静默丢掉。
        self._root = root
        self._loop = asyncio.get_running_loop()
        self._budget = _Budget(_INFLIGHT_BUDGET_BYTES)
        self._wake = asyncio.Event()
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(self._root), recursive=True)
        self._observer.start()
        self._drain = asyncio.create_task(self._drain_loop())
        logger.info("[myspace-registry] 开始监听 %s", self._root)

    async def stop(self) -> None:
        self._closed = True
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            await asyncio.to_thread(observer.join, 5)
        self._wake.set()  # 叫醒挂在事件上的去抖循环，让它看见 _closed
        if self._drain is not None:
            self._drain.cancel()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        logger.info("[myspace-registry] 已停止")

    # ───── 事件接入 ────────────────────────────────────────────────────────

    def _on_event(self, event) -> None:  # noqa: ANN001 — 在 watchdog 线程里跑
        if self._closed or self._loop is None or self._root is None:
            return
        # 目录事件里只有删除和改名有意义：新建目录本身不是用户文件，删除则可能是整个
        # 文件夹被 rm -rf 掉了，里面每个文件的删除事件未必都到得齐。
        if event.is_directory and event.event_type not in ("deleted", "moved"):
            return
        for raw in (event.src_path, getattr(event, "dest_path", None)):
            if not raw:
                continue
            target = _split(str(raw), self._root)
            if target is None:
                continue
            self._loop.call_soon_threadsafe(self._mark, target)

    def _mark(self, key: tuple[str, str]) -> None:
        self._pending[key] = time.monotonic()
        self._wake.set()

    async def _drain_loop(self) -> None:
        """把安静下来的路径交出去处理。同一路径同时只处理一次。

        没有待处理的改动时**完全不转**，挂在事件上等；有待处理的就正好睡到最早那一条
        安静够为止。定频轮询在没人动文件的时候也一直醒，纯属白烧。
        """
        while not self._closed:
            try:
                if not self._pending:
                    await self._wake.wait()
                    self._wake.clear()
                    continue
                now = time.monotonic()
                ready = [k for k, ts in self._pending.items() if now - ts >= _SETTLE_S]
                if not ready:
                    await asyncio.sleep(max(_SETTLE_S - (now - min(self._pending.values())), 0.05))
                    continue
                self._dispatch(ready, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 这条循环不能倒
                logger.warning("[myspace-registry] 去抖循环出错: %s", exc)
                await asyncio.sleep(_ERROR_BACKOFF_S)

    def _dispatch(
        self, ready: list[tuple[str, str]], now: float, *, force: bool = False
    ) -> list[asyncio.Task]:
        """按用户成批派发：同一批共用一次会话查询和一次判定，不逐个文件重来。"""
        by_user: dict[str, list[str]] = {}
        for key in ready:
            self._pending.pop(key, None)
            if key in self._inflight:
                self._pending[key] = now  # 上一次还没跑完，等下一轮
                continue
            self._inflight.add(key)
            by_user.setdefault(key[0], []).append(key[1])
        started: list[asyncio.Task] = []
        for user_id, rels in by_user.items():
            task = asyncio.create_task(self._guarded(user_id, rels, force=force))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started.append(task)
        return started

    async def _guarded(self, user_id: str, rels: list[str], *, force: bool = False) -> None:
        try:
            await self._process(user_id, rels, force=force)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 一批出错不该拖累其它用户
            logger.warning(
                "[myspace-registry] 处理 user=%s 的 %d 个改动失败: %s", user_id, len(rels), exc
            )
        finally:
            for rel in rels:
                self._inflight.discard((user_id, rel))

    # ───── 一批改动 ────────────────────────────────────────────────────────

    async def _register(self, user_id: str, entry) -> None:
        """把一个文件登记进账本。只有这一步把文件内容读进内存，所以只有这一步占内存额度。"""
        assert self._budget is not None
        async with self._budget.reserve(entry.size):
            if await asyncio.to_thread(mirror.register_entry, user_id=user_id, entry=entry):
                logger.info("[myspace-registry] 登记 %s user=%s", entry.rel, user_id)

    async def _process(self, user_id: str, rels: list[str], *, force: bool = False) -> None:
        """处理这个用户这一批改动。

        顺序是「先认领，再判定」：判定要查库，而每个 worker 都看得见同一批事件，先判定
        就等于 N 个 worker 把同一批库查了 N 遍，只有一个算数。
        """
        ask = _Ask(user_id)
        if not force and await ask.get() is None:
            # 只有托管着这个用户会话的那个进程弹得出确认条，让它先来。
            await asyncio.sleep(_HANDOFF_GRACE_S)

        entries = await asyncio.to_thread(_stat_all, user_id, rels)
        # 文件还在的按 mtime 认领；已经没了的留给删除那条路按 artifact id 认领（同一个
        # 路径反复删了又建，用固定串认领会把第二次删除挡在 TTL 里）。
        writes = [(rel, e) for rel, e in entries.items() if e is not None]
        gone = [rel for rel, e in entries.items() if e is None]
        won = await asyncio.gather(
            *(_claim(user_id, rel, str(int(e.mtime * 1000))) for rel, e in writes)
        )
        claimed = {rel: e for (rel, e), ok in zip(writes, won) if ok}
        if gone:
            await self._apply_deletes(user_id, gone, ask)
        if not claimed:
            return

        verdicts = await asyncio.to_thread(
            mirror.classify_claimed, user_id=user_id, entries=claimed
        )
        fresh = []
        for rel, verdict in verdicts.items():
            entry = claimed[rel]
            if verdict == mirror.VERDICT_NEW:
                fresh.append(entry)
            elif verdict == mirror.VERDICT_TOO_LARGE:
                logger.info("[myspace-registry] 超过大小上限不登记 %s/%s", user_id, rel)
            elif verdict == mirror.VERDICT_MODIFIED:
                # 要问人的逐个来：一次弹一排确认条比串行慢那点更难受。
                if await self._approved(
                    user_id=user_id,
                    ask=ask,
                    logical_path=entry.logical_path,
                    op=OP_EDIT,
                    summary=f"沙盒改写了「我的空间」里已有的文件：{rel}",
                ):
                    await self._register(user_id, entry)
                else:
                    await asyncio.to_thread(mirror.restore_from_registry, user_id=user_id, rel=rel)
            # VERDICT_CURRENT：账本已经反映了这份内容。VERDICT_STALE：用户已经删掉它了，
            # 磁盘上这份是残留副本 —— 登记等于把用户的清理撤销，自动删又可能丢掉对象存储
            # 里没有副本的内容，交给人工对账脚本决定。
        if fresh:
            # 新文件不需要问人，可以并发登记：一条命令落几十个文件时串行等于把几十次
            # 存储往返串起来。并发度由内存额度决定。
            await asyncio.gather(*(self._register(user_id, e) for e in fresh))

    async def _apply_deletes(self, user_id: str, rels: list[str], ask: "_Ask") -> None:
        """磁盘上没了的这些路径，该从账本里删的一起删掉。"""
        targets = await asyncio.to_thread(mirror.classify_deletes, user_id=user_id, rels=rels)
        for rel, target in targets.items():
            if target is None:
                continue  # 没登记过的东西消失了，本来就不在用户空间里
            if not await _claim(user_id, rel, f"del:{target.stamp}"):
                continue
            if not await self._approved(
                user_id=user_id,
                ask=ask,
                logical_path=f"{MYSPACE_LOGICAL}/{rel}",
                op=OP_DELETE,
                summary=f"沙盒删除了「我的空间」里的{target.kind_label}：{rel}",
            ):
                if target.registered is not None:
                    await asyncio.to_thread(
                        mirror.restore_from_registry,
                        user_id=user_id,
                        rel=rel,
                        reg=target.registered,
                    )
                # 文件夹没法从对象存储整目录还原；里面的文件在账本里还在，下一轮正向同步
                # （pull_myspace_updates）会把它们拉回镜像。
                continue
            await asyncio.to_thread(mirror.delete_registered, user_id=user_id, rel=rel)

    async def _approved(
        self,
        *,
        user_id: str,
        ask: "_Ask",
        logical_path: str,
        op: str,
        summary: str,
    ) -> bool:
        """要不要让这次改动落进账本。问不到人、或用户的权限档已经替他答过，就直接放行。"""
        chat_id = await ask.get()
        if chat_id is None:
            # 没有活跃会话、或者写的是子智能体/批量/定时任务 —— 文件在磁盘上早就改了，
            # 此时拒绝拦不住已经发生的事，只会让界面和沙箱两边不一致。
            return True
        from core.llm.tool_permissions import preset_answers_for_user

        # 按 op 问：``delete`` 属于危险操作，和 ``edit`` 在同一个档位下的答案未必相同，
        # 所以这个判定不能整批缓存一次了事。
        if await asyncio.to_thread(preset_answers_for_user, user_id, op=op):
            return True
        from core.llm.tools import _myspace_confirm as mc

        res = await mc.gate(
            chat_id=chat_id,
            op=op,
            logical_path=logical_path,
            interactive=True,
            summary=summary,
            timeout=_CONFIRM_TIMEOUT_S,
        )
        return res is None

    async def flush(self, user_id: str, *, timeout: float = 5.0) -> None:
        """把这个用户手上待处理的改动立刻处理掉，不等去抖窗口。

        模型写完文件马上 ``list_myspace_files``、用户写完就打开「我的空间」，都不该等上
        那一两秒。这不是第二条登记路径 —— 还是同一个登记器，只是催它快一点，认领依旧保证
        一次改动只落一次。
        """
        keys = [k for k in list(self._pending) if k[0] == user_id and k not in self._inflight]
        if not keys:
            return
        started = self._dispatch(keys, time.monotonic(), force=True)
        if not started:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*started, return_exceptions=True), timeout)
        except asyncio.TimeoutError:
            logger.info("[myspace-registry] 催办 user=%s 超时，剩下的按常规节奏走", user_id)


_registry: Optional[MySpaceRegistry] = None


async def start_registry() -> None:
    global _registry
    if _registry is not None:
        return
    _registry = MySpaceRegistry()
    await _registry.start()


def get_registry() -> Optional[MySpaceRegistry]:
    """当前进程的登记器；没起来时返回 ``None``（后台 worker 健康报告读它）。"""
    return _registry


async def flush_user(user_id: str) -> None:
    """催一下这个用户待登记的改动 —— 读「我的空间」之前调，看到的就是当下状态。"""
    if _registry is None or not user_id:
        return
    try:
        await _registry.flush(user_id)
    except Exception as exc:  # noqa: BLE001 — 催办失败只是慢一点，不该拦住读取
        logger.warning("[myspace-registry] 催办失败 user=%s: %s", user_id, exc)


async def stop_registry() -> None:
    global _registry
    if _registry is None:
        return
    await _registry.stop()
    _registry = None
