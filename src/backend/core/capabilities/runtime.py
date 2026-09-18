"""Durable desktop run bindings and per-run skill views in the existing business DB.

Snapshots pin full content hashes and revisions. They are retained with history;
there is deliberately no automatic revision collector until retention policy is
explicit. Authorization is rechecked before exposing the frozen files.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from core.db.models import ContentBlock

from . import archive, registry, skills, store, view
from .errors import (
    CapabilityError,
    IntegrityFailed,
    PackageMissing,
    PermissionDenied,
    ViewUnavailable,
)
from .paths import BUILTIN_PROFILE, KIND_SKILL, LOCAL_PROFILE, require_root, revision_for_hash

_lock = threading.RLock()
_PREFIX = "desktop_capability_run:"


def _key(run_id):
    return hashlib.sha256(str(run_id).encode()).hexdigest()


def child_scope(parent_scope: str, kind: str, *identities: str) -> str:
    """Bounded, deterministic scope from durable orchestration identities."""
    if not kind or not identities or any(not str(value or "").strip() for value in identities):
        raise IntegrityFailed("capability scope requires durable invocation identities")
    material = json.dumps(
        [str(parent_scope or ""), str(kind), *map(str, identities)], separators=(",", ":")
    )
    return "s_" + _key(material)


def _snapshot_key(run_id, scope_id=""):
    # Preserve every existing main-run ContentBlock and view key byte-for-byte.
    return (
        _key(run_id)
        if not scope_id
        else _key(json.dumps([str(run_id), str(scope_id)], separators=(",", ":")))
    )


@dataclass(frozen=True)
class PreparedRun:
    run_id: str
    user_id: str
    profile: Optional[str]
    execution_plane: str
    bindings: dict[str, dict[str, Any]]
    mcp_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_frozen: bool = False
    authorization_fingerprint: Optional[str] = None
    dependency_report: dict = field(default_factory=dict)
    scope_id: str = ""
    unavailable: dict = field(default_factory=dict)

    @property
    def view_dir(self):
        return (
            require_root()
            / ".capabilities"
            / "views"
            / _snapshot_key(self.run_id, self.scope_id)
            / "skills"
        )

    def to_dict(self):
        return {
            "run_id": self.run_id,
            "scope_id": self.scope_id,
            "unavailable": copy.deepcopy(self.unavailable),
            "user_id": self.user_id,
            "profile": self.profile,
            "execution_plane": self.execution_plane,
            "bindings": copy.deepcopy(self.bindings),
            "mcp_bindings": copy.deepcopy(self.mcp_bindings),
            "mcp_frozen": self.mcp_frozen,
            "authorization_fingerprint": self.authorization_fingerprint,
            "dependency_report": copy.deepcopy(self.dependency_report),
        }


def get(run_id: str, scope_id: str = "") -> Optional[PreparedRun]:
    with registry._session() as db:
        row = db.get(ContentBlock, _PREFIX + _snapshot_key(run_id, scope_id))
        return PreparedRun(**copy.deepcopy(row.payload)) if row else None


def _component(binding):
    return store.get(KIND_SKILL, binding["profile"], binding["key"], binding["revision"])


def _validate_skill_binding(name: str, binding: dict, run: PreparedRun) -> None:
    """Re-check one frozen skill: still authorized (enabled/owner) and its bytes
    intact (content hash). Runs on every enumeration so a mid-session disable or
    an out-of-band edit of the frozen file is caught before a read.

    摘要走内容指纹缓存，缓存键是每个文件的元数据签名：任何字节改动都会让它失效并
    重新逐字节读一遍，所以篡改照样能抓到，而没变过的视图一次磁盘读都不用。
    """
    if binding["profile"] != BUILTIN_PROFILE:
        inst = registry.get(binding["install_id"])
        if inst is None or inst.state == "removed" or not inst.enabled:
            raise PermissionDenied("capability is no longer authorized", runtime_name=name)
        owner = inst.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("capability belongs to another user", runtime_name=name)
    comp = _component(binding)
    for iid in binding.get("dependency_install_ids", []):
        if iid.split(":", 2)[1] == BUILTIN_PROFILE:
            continue
        dependency = registry.get(iid)
        if dependency is None or dependency.state == "removed" or not dependency.enabled:
            raise PermissionDenied("skill dependency is no longer authorized", runtime_name=name)
        owner = dependency.payload.get("owner_user_id")
        if owner and str(owner) != run.user_id:
            raise PermissionDenied("skill dependency belongs to another user", runtime_name=name)
    if comp is None or skills.skill_dir_hash(comp.path) != binding["content_hash"]:
        raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)


def validate(
    run: PreparedRun,
    *,
    user_id: Optional[str] = None,
    execution_plane: str = "local",
    only_skill: Optional[str] = None,
) -> None:
    """Verify a prepared run is still authorized and its frozen bytes intact.

    带 ``only_skill`` 时只复查那一个技能绑定加上廉价的账号守卫。技能枚举按这个
    参数逐个复查自己那一份：N 次工作量，而不是每个加载器都把全量技能重算一遍的
    N² ——后者曾是桌面端装配最大的一块开销。整轮的视图校验在 ``rebuild`` 里做。
    """
    if user_id is not None and run.user_id != str(user_id):
        raise PermissionDenied("prepared run belongs to another user")
    if run.execution_plane != execution_plane:
        raise PackageMissing(
            "prepared execution plane changed; prepare a new run",
            details={"recovery_action": "switch_execution_plane"},
        )
    if run.profile is not None:
        from core.services.desktop_cloud_bridge import (
            _state_fingerprint,
            ensure_current_authorization,
            get_state,
        )

        if (
            run.profile != skills.current_account_profile()
            or run.authorization_fingerprint != _state_fingerprint(get_state())
        ):
            raise PermissionDenied("cloud session changed; prepare a new run")
        if only_skill is None and (
            any(b["profile"] not in (BUILTIN_PROFILE, LOCAL_PROFILE) for b in run.bindings.values())
            or any(
                b["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, "local-json")
                for b in run.mcp_bindings.values()
            )
            or any(
                node["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, BUILTIN_PROFILE)
                for node in run.dependency_report.get("nodes", [])
            )
        ):
            ensure_current_authorization()
    # 不带 ``only_skill`` 时到此为止：组件级授权由读取方与执行视图各自把关，
    # 被撤销的单个组件只摘掉自己，不牵连这一轮里无关的工具或纯文本。
    if only_skill is None:
        return
    latest = get(run.run_id, scope_id=run.scope_id) or run
    if only_skill in latest.unavailable:
        raise PackageMissing("skill is unavailable in this view", runtime_name=only_skill)
    binding = run.bindings.get(only_skill)
    if binding is None:
        raise PermissionDenied("capability is no longer authorized", runtime_name=only_skill)
    _validate_skill_binding(only_skill, binding, run)


def validate_activation(run: PreparedRun, nodes, *, available_mcp=()) -> None:
    """激活一个此前被推迟的插件之前，复查它自己。

    延迟加载把插件的定义留到模型真正要用时才展开，这中间用户可能已经把它停用、
    卸载，或者定义文件被改过；它声明的版本 / 平台约束也要拿这一轮**冻结的那个**
    技能版本去对，而不是拿当前安装的版本。``nodes`` 是推迟那一刻记下的身份。
    只查这一个插件及其定义闭包，不牵连整轮。
    """
    from .dependency import Context, Inspector, _identifier, component_hash, require_report

    nodes = list(nodes or [])
    if not nodes:
        return
    for node in nodes:
        kind, profile, key = node["install_id"].split(":", 2)
        if profile != BUILTIN_PROFILE:
            inst = registry.get(node["install_id"])
            if inst is None or inst.state == "removed" or not inst.enabled:
                raise PermissionDenied("capability is no longer authorized", runtime_name=key)
            owner = inst.payload.get("owner_user_id")
            if owner and str(owner) != run.user_id:
                raise PermissionDenied("capability belongs to another user", runtime_name=key)
        comp = store.get(kind, profile, key, node["revision"])
        if comp is None or component_hash(comp) != node["content_hash"]:
            raise IntegrityFailed(
                "prepared dependency revision is missing or changed", runtime_name=key
            )

    # 这一轮没选中的组件跳过；选中的那些，约束必须在冻结版本上依然成立。
    selected_mcp = set(available_mcp or ())

    def is_selected(entry, _required):
        key = _identifier(entry).split(":")[-1]
        if entry.get("kind") == "skill":
            return key in run.bindings
        if entry.get("kind") == "mcp":
            return key in selected_mcp
        return True

    inspector = Inspector(
        Context(
            user_id=run.user_id,
            bindings=run.bindings,
            available_mcp=selected_mcp,
            frozen_nodes={node["install_id"]: node for node in nodes},
            collect_hashes=False,
        ),
        on_visit=is_selected,
    )
    inspector.visit_roots(
        [
            ({"kind": node["kind"], "id": node["install_id"]}, node["install_id"].split(":", 2)[1])
            for node in nodes
        ]
    )
    require_report(inspector.report())


def rebuild(run: PreparedRun) -> Path:
    """把执行视图链到冻结的版本目录上。

    视图是派生物，随时可以从绑定重建；这里只创建和删除链接，绝不复制字节。
    某个组件通不过校验就把它从视图里摘掉并记进 ``unavailable``，其余照常可用。
    """
    run = get(run.run_id, scope_id=run.scope_id) or run
    fingerprint = _view_fingerprint(run)
    unavailable = dict(run.unavailable)
    targets: Dict[str, Path] = {}
    for name, binding in run.bindings.items():
        if name in unavailable:
            continue
        try:
            _validate_skill_binding(name, binding, run)
            component = _component(binding)
            if component is None:
                raise IntegrityFailed("prepared revision is missing or changed", runtime_name=name)
            targets[name] = component.path
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "view_unavailable")
    report = view.build_view(run.view_dir, targets, allowed_roots=[require_root()])
    if report.blocked or report.foreign:
        # 名字被真实目录占用或大小写撞名：沙箱绝不能收到一个来路不明的目录。
        raise ViewUnavailable(
            "prepared view is blocked",
            details={"blocked": report.blocked, "foreign": report.foreign},
        )
    if unavailable != run.unavailable:
        run = save(replace(run, unavailable=unavailable))
    with _view_build_lock:
        _view_built[(run.run_id, run.scope_id)] = fingerprint
    return run.view_dir


def _freeze_candidate(name, candidate):
    # 摘要走内容指纹缓存：候选路径是不可变存储里的版本目录，字节一改元数据签名
    # 就变、缓存随之失效，所以复用安全，而重复冻结同一版本不必再读一遍磁盘。
    actual = skills.skill_dir_hash(candidate.path)
    revision = candidate.revision or revision_for_hash(actual)
    profile = candidate.profile
    if profile == BUILTIN_PROFILE:
        if store.get(KIND_SKILL, profile, name, revision) is None:
            files = {
                rel: path.read_bytes()
                for rel, path in archive.iter_files(candidate.path)
                if rel != ".inventory.json"
            }
            store.write_from_files(KIND_SKILL, profile, name, revision, files)
    elif candidate.content_hash and actual != candidate.content_hash:
        raise IntegrityFailed("installed content changed", runtime_name=name)
    installation = None
    if profile != BUILTIN_PROFILE:
        installation = registry.get(candidate.install_id)
    return {
        "install_id": candidate.install_id,
        "profile": profile,
        "key": candidate.ref.key if candidate.ref else candidate.install_id.split(":", 2)[2],
        "revision": revision,
        "content_hash": actual,
        "resource_ref": candidate.ref.to_dict() if candidate.ref else None,
        "version": installation.version if installation else "",
    }


def _definition_data(definition):
    if hasattr(definition, "to_serialized"):
        return definition.to_serialized()
    return {
        key: copy.deepcopy(getattr(definition, key, None))
        for key in (
            "skill_ids",
            "mcp_server_ids",
            "plugin_ids",
            "kb_ids",
            "model_provider_id",
            "extra_config",
            "dependencies",
            "platforms",
            "extensions",
        )
    }


def _bind_cloud_identity(run):
    from core.services.desktop_cloud_bridge import (
        _state_fingerprint,
        ensure_current_authorization,
        get_state,
    )

    if not skills.account_authorized_for(run.user_id):
        raise PermissionDenied("cloud capabilities belong to another user")
    profile = skills.current_account_profile()
    fingerprint = _state_fingerprint(get_state())
    if not profile:
        raise PermissionDenied("cloud session is unavailable")
    if run.profile is not None and (
        run.profile != profile or run.authorization_fingerprint != fingerprint
    ):
        raise PermissionDenied("cloud session changed; prepare a new run")
    ensure_current_authorization()
    return replace(run, profile=profile, authorization_fingerprint=fingerprint)


def save(run: PreparedRun) -> PreparedRun:
    """Persist the run snapshot; the business DB row is its durable record."""
    with registry._session() as db:
        key = _PREFIX + _snapshot_key(run.run_id, run.scope_id)
        row = db.get(ContentBlock, key)
        if row is None:
            db.add(ContentBlock(id=key, payload=run.to_dict()))
        else:
            row.payload = run.to_dict()
    return run


def prepare(
    run_id: str,
    user_id: str,
    *,
    skill_ids=None,
    execution_plane="local",
    agent_definition=None,
    plugin_ids=(),
    scope_id: str = "",
) -> PreparedRun:
    """冻结这一轮可用的能力闭包。

    冻结不是复制：每个组件都钉在不可变存储里的一个版本上，版本按内容指纹划分且
    永不原地修改，所以编辑安装目录只会产生新版本，已冻结的这一轮看到的字节自始
    至终不变。准备不了的单个组件记进 ``unavailable``，不影响其余组件。
    """
    from .preparation import ensure_cloud_ready

    if not run_id:
        raise ValueError("a run id is required for a durable capability snapshot")
    previous = get(run_id, scope_id=scope_id)
    if previous is not None:
        validate(previous, user_id=user_id, execution_plane=execution_plane)
        # 重放这一轮只能用它当初冻下来的那些；子智能体想要更多，得开新的一轮，
        # 不能借同一个 run 键把闭包悄悄撑大。
        missing = set(skill_ids or []) - set(previous.bindings) - set(previous.unavailable)
        if missing:
            raise PackageMissing(
                "requested skills are absent from the frozen run",
                details={"skills": sorted(missing)},
            )
        rebuild(previous)
        return get(run_id, scope_id=scope_id) or previous
    if execution_plane != "local":
        raise PackageMissing("device capabilities require local execution")
    requested = list(
        dict.fromkeys([*(skill_ids or []), *(getattr(agent_definition, "skill_ids", None) or [])])
    )
    # 这一轮的根：选中的技能，加上选中的插件。先各自按需下载，再从它们出发
    # 走一遍依赖，把闭包里牵连到的技能也收进来。一个根准备不了只记下它自己。
    account = skills.current_account_profile() or LOCAL_PROFILE
    roots = [("skill", name, LOCAL_PROFILE, name) for name in requested]
    roots += [("plugin", key, account, "plugin:" + key) for key in plugin_ids]
    unavailable = {}
    for kind, key, _profile, label in roots:
        keys = {"skill_keys": [key]} if kind == "skill" else {"plugin_keys": [key]}
        try:
            ensure_cloud_ready(user_id, **keys)
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[label] = getattr(exc, "code", "package_missing")
    resolution = skills.resolve_for_user(str(user_id))
    from .dependency import Context
    from .readiness import _BindingInspector, _choice_bindings

    discovery = _BindingInspector(
        Context(
            user_id=str(user_id), collect_hashes=False, bindings=_choice_bindings(resolution.chosen)
        )
    )
    for kind, key, profile, _label in roots:
        try:
            discovery.visit({"kind": kind, "id": key}, profile)
        except (CapabilityError, OSError, ValueError):
            continue
    if agent_definition is not None:
        discovery.definition(
            _definition_data(agent_definition),
            kind="agent",
            profile=getattr(agent_definition, "profile", LOCAL_PROFILE),
            label="agent:" + str(agent_definition.agent_id),
        )
    requested = list(
        dict.fromkeys(
            [
                *requested,
                *(
                    node["install_id"].split(":", 2)[2]
                    for node in discovery.nodes.values()
                    if node["kind"] == "skill"
                ),
            ]
        )
    )
    bindings = {}
    for name in requested:
        candidate = resolution.chosen.get(name)
        if candidate is None:
            unavailable[name] = (
                "name_conflict" if name in resolution.conflicts else "package_missing"
            )
            continue
        try:
            bindings[name] = _freeze_candidate(name, candidate)
            unavailable.pop(name, None)
        except (CapabilityError, OSError, ValueError) as exc:
            unavailable[name] = getattr(exc, "code", "package_missing")
    run = PreparedRun(
        str(run_id),
        str(user_id),
        None,
        execution_plane,
        bindings,
        scope_id=str(scope_id or ""),
        unavailable=unavailable,
    )
    if any(b["profile"] not in (LOCAL_PROFILE, BUILTIN_PROFILE) for b in bindings.values()):
        run = _bind_cloud_identity(run)
    save(run)
    rebuild(run)
    return get(run_id, scope_id=scope_id) or run


_view_build_lock = threading.Lock()
# (run_id, scope_id) -> fingerprint of the frozen skill set last materialized.
# The frozen store is content-addressed, so an unchanged fingerprint means the
# junction view on disk is already correct; rebuilding it (deep validate + all
# junctions) on every agent assembly was pure per-session waste. A capability
# change re-resolves to new revisions → new fingerprint → rebuild.
_view_built: dict[tuple[str, str], tuple] = {}


def _view_fingerprint(run: PreparedRun) -> tuple:
    return (
        skills.view_generation(),
        tuple(sorted((name, b.get("revision")) for name, b in run.bindings.items())),
    )


def _ensure_view(run: PreparedRun) -> None:
    """Materialize the run's view only when its frozen skill set actually moved."""
    key = (run.run_id, run.scope_id)
    fingerprint = _view_fingerprint(run)
    with _view_build_lock:
        already = _view_built.get(key) == fingerprint
    if not already or not run.view_dir.exists():
        rebuild(run)


def frozen_loader(run: PreparedRun):
    from core.agent_skills.backends import CompositeBackend, FilesystemBackend
    from core.agent_skills.loader import MultiSourceSkillLoader

    _ensure_view(run)
    # 视图重建可能刚摘掉某个组件，读取方必须拿到落库后的最新 ``unavailable``。
    run = get(run.run_id, scope_id=run.scope_id) or run
    loader = MultiSourceSkillLoader(CompositeBackend([FilesystemBackend(run.view_dir, "prepared")]))
    loader.capability_run = run
    return loader


def view_for_execution(run_id: str, user_id: str, scope_id: str = "") -> Optional[Path]:
    run = get(run_id, scope_id=scope_id)
    if run is None:
        return None
    validate(run, user_id=user_id)
    return rebuild(run)


def _config_digest(config):
    # Connection instructions are frozen; secret inputs may rotate independently.
    secret_names = (
        "TOKEN",
        "SECRET",
        "PASSWORD",
        "API_KEY",
        "APIKEY",
        "CREDENTIAL",
        "AUTHORIZATION",
        "COOKIE",
    )

    def public_values(values):
        return {
            key: value
            for key, value in (values or {}).items()
            if not any(marker in str(key).upper().replace("-", "_") for marker in secret_names)
        }

    safe = {
        key: val
        for key, val in config.items()
        if key not in {"headers", "env", "manifest_tools", "manifest_revision", "schema_hash"}
    }
    safe["headers"] = public_values(config.get("headers"))
    safe["env"] = public_values(config.get("env"))
    return hashlib.sha256(json.dumps(safe, sort_keys=True, default=str).encode()).hexdigest()


def bind_mcp(run: PreparedRun, configs, choices):
    """Freeze MCP source identities/contracts; refresh credentials independently."""
    from .connectors import server_id_of

    selected = {server_id_of(c): c.install_id for c in choices.chosen.values()} if choices else {}
    current = {
        sid: {
            "install_id": selected.get(sid, "mcp:local:" + sid),
            "authorization_checked": sid in selected,
            "config_digest": _config_digest(config),
            "manifest_tools": copy.deepcopy(config.get("manifest_tools")),
            "schema_hash": config.get("schema_hash"),
            "manifest_revision": config.get("manifest_revision"),
        }
        for sid, config in configs.items()
    }
    with _lock:
        saved = get(run.run_id, scope_id=run.scope_id)
        validate(saved or run)
        pinned = (saved or run).mcp_bindings
        if not (saved or run).mcp_frozen:
            pinned = current
            frozen = replace(saved or run, mcp_bindings=pinned, mcp_frozen=True)
            if any(
                entry["install_id"].split(":", 2)[1] not in (LOCAL_PROFILE, "local-json")
                for entry in pinned.values()
            ):
                frozen = _bind_cloud_identity(frozen)
            with registry._session() as db:
                row = db.get(ContentBlock, _PREFIX + _snapshot_key(run.run_id, run.scope_id))
                row.payload = frozen.to_dict()
        else:
            # A sub-agent may use a subset; expanding outside the parent's
            # frozen bindings requires a new run, not an implicit source swap.
            unavailable = dict((saved or run).unavailable)
            for sid, entry in list(current.items()):
                old = pinned.get(sid)
                if (
                    old is None
                    or old["install_id"] != entry["install_id"]
                    or old["config_digest"] != entry["config_digest"]
                ):
                    # Keep the old source contract; never reconnect a changed
                    # endpoint as though it were the original tool.
                    unavailable["mcp:" + sid] = "connector_changed"
                    configs = {key: value for key, value in configs.items() if key != sid}
            if unavailable != (saved or run).unavailable:
                save(replace(saved or run, unavailable=unavailable))
        # Pinned schemas are persisted and never edited afterwards; sharing them
        # with the assembled config is safe and avoids re-copying every tool schema.
        return {
            sid: {
                **config,
                **{
                    key: pinned[sid][key]
                    for key in ("manifest_tools", "schema_hash", "manifest_revision")
                    if pinned[sid].get(key) is not None
                },
            }
            for sid, config in configs.items()
        }


def pin_agent_definition(run_id, user_id, definition, *, scope_id: str = ""):
    """Keep the selected agent's instructions and dependency IDs stable on replay."""
    from .agents import _JSON_FIELDS, AgentDefinition

    ident = str(definition.agent_id)
    key = "desktop_capability_agent:" + _snapshot_key(str(run_id) + ":" + ident, scope_id)
    profile = (
        skills.current_account_profile()
        if getattr(definition, "origin", "local") == "cloud"
        else None
    )
    from core.services.desktop_cloud_bridge import (
        _state_fingerprint,
        ensure_current_authorization,
        get_state,
    )

    fingerprint = _state_fingerprint(get_state()) if profile else None
    if getattr(definition, "origin", "local") == "cloud":
        if not skills.account_authorized_for(user_id):
            raise PermissionDenied("cloud agent belongs to another user")
        ensure_current_authorization()
        current = registry.get(
            registry.install_id("agent", str(getattr(definition, "profile", profile)), ident)
        )
        if current is None or not current.ready or not current.enabled:
            raise PermissionDenied("cloud agent is no longer authorized")
    if not getattr(definition, "is_enabled", True):
        raise PermissionDenied("selected agent is disabled")
    with _lock, registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            data = copy.deepcopy(row.payload)
            if (
                data["user_id"] != str(user_id)
                or data["profile"] != profile
                or data.get("authorization_fingerprint") != fingerprint
            ):
                raise PermissionDenied("prepared agent belongs to another account")
            return AgentDefinition.from_serialized(data["definition"])
        fields = {
            name: copy.deepcopy(getattr(definition, name, None))
            for name in _JSON_FIELDS
            if hasattr(definition, name)
        }
        fields.update(
            {
                "system_prompt": str(getattr(definition, "system_prompt", "") or ""),
                "user_id": getattr(definition, "user_id", None),
                "origin": getattr(definition, "origin", "local"),
                "profile": getattr(definition, "profile", LOCAL_PROFILE),
                "revision": getattr(definition, "revision", None),
            }
        )
        frozen = AgentDefinition.from_serialized(fields)
        db.add(
            ContentBlock(
                id=key,
                payload={
                    "run_id": str(run_id),
                    "scope_id": str(scope_id or ""),
                    "user_id": str(user_id),
                    "profile": profile,
                    "authorization_fingerprint": fingerprint,
                    "definition": fields,
                },
            )
        )
        return frozen


def references(kind: str, profile: str, key: str, revision: Optional[str] = None) -> list[str]:
    """History retains exact skill, plugin and agent revisions until explicitly purged."""
    target = registry.install_id(kind, profile, key)
    with registry._session() as db:
        rows = db.query(ContentBlock).filter(ContentBlock.id.startswith(_PREFIX)).all()
        result = []
        for row in rows:
            entries = list((row.payload.get("bindings") or {}).values()) + (
                row.payload.get("dependency_report") or {}
            ).get("nodes", [])
            if any(
                entry.get("install_id") == target
                and (revision is None or entry.get("revision") == revision)
                for entry in entries
            ):
                result.append(str(row.payload["run_id"]))
        if kind == "agent":
            rows = (
                db.query(ContentBlock)
                .filter(ContentBlock.id.startswith("desktop_capability_agent:"))
                .all()
            )
            for row in rows:
                data = row.payload.get("definition") or {}
                if (
                    data.get("agent_id") == key
                    and data.get("profile", LOCAL_PROFILE) == profile
                    and (revision is None or data.get("revision") == revision)
                ):
                    if row.payload.get("run_id"):
                        result.append(str(row.payload["run_id"]))
        return sorted(set(result))


def _persist_preflight_report(run, report, *, offered=()):
    """Freeze identity once ready while continuing to publish current readiness.

    ``offered`` names the nodes that only the ambient catalog contributed. A
    ready scope must never swap the closure it *committed* to, but the catalog
    it offers may legitimately grow — a skill whose connector arrived becomes
    usable — so a newcomer is pinned rather than treated as tampering.
    """
    with _lock:
        latest = get(run.run_id, scope_id=run.scope_id) or run
        prior = latest.dependency_report
        frozen = bool(prior.get("frozen") or prior.get("ready"))
        changed = False
        report = copy.deepcopy(report)
        if frozen:
            offered = set(offered)
            pinned = {node["install_id"]: node for node in prior.get("nodes", [])}
            for node in report.get("nodes", []):
                previous = pinned.get(node["install_id"])
                if previous is None:
                    if node["install_id"] not in offered:
                        changed = True
                    else:
                        pinned[node["install_id"]] = node
                elif any(
                    previous.get(field) != node.get(field)
                    for field in ("kind", "revision", "content_hash")
                ):
                    changed = True
            report["nodes"] = copy.deepcopy(list(pinned.values()))
        if changed:
            report["ready"] = False
            report.setdefault("errors", []).append(
                {
                    "code": "scope_selection_changed",
                    "dependency_chain": [],
                    "recovery_action": "prepare_new_scope",
                }
            )
        report["frozen"] = frozen or bool(report.get("ready"))
        report["state"] = "ready" if report.get("ready") else "blocked"
        if not report.get("ready"):
            report["error"] = {
                "code": "integrity_failed" if changed else "dependency_missing",
                "recovery_action": "prepare_new_scope" if changed else "inspect_dependencies",
            }
        updated = replace(latest, dependency_report=report)
        if latest.profile is None and run.profile is not None:
            updated = replace(
                updated,
                profile=run.profile,
                authorization_fingerprint=run.authorization_fingerprint,
            )
        with registry._session() as db:
            row = db.get(ContentBlock, _PREFIX + _snapshot_key(run.run_id, run.scope_id))
            row.payload = updated.to_dict()
        if changed:
            raise IntegrityFailed("capability selection changed; prepare a new scope")
        return updated


def preflight(
    run,
    *,
    available_mcp=(),
    available_kb=(),
    available_models=None,
    plugin_ids=(),
):
    """走一遍依赖闭包，把这一轮真正能跑的能力定下来。

    某个技能的依赖在本机不成立，就只把这个技能摘掉并记进 ``unavailable_skills``，
    绝不因此让整个助手不可用。连接器与插件的从属关系记进 ``connector_parents``，
    供「加载插件」一类的工具反查。
    """
    from .dependency import Context, Inspector

    validate(run)
    run = get(run.run_id, scope_id=run.scope_id) or run
    if run.dependency_report.get("ready"):
        # 这个作用域已经定过一次，就保持那份冻结报告，只把视图对齐。
        rebuild(run)
        return get(run.run_id, scope_id=run.scope_id) or run
    unavailable = dict(run.unavailable)
    context = Context(
        user_id=run.user_id,
        bindings=run.bindings,
        available_mcp=set(available_mcp),
        available_kb=set(available_kb),
        available_models=available_models,
        collect_hashes=False,
    )
    nodes = {}
    connector_parents = {}

    class _ParentTrackingInspector(Inspector):
        def external(self, entry, chain, required):
            if entry.get("kind") == "mcp":
                connector_parents.setdefault(chain[-1][4:], []).extend(
                    parent for parent in chain[:-1] if parent.startswith("plugin:")
                )
            return super().external(entry, chain, required)

    for name, binding in run.bindings.items():
        if name in unavailable:
            continue
        inspector = _ParentTrackingInspector(context)
        try:
            inspector.visit({"kind": "skill", "id": name}, binding["profile"])
            if inspector.errors:
                unavailable[name] = "dependency_missing"
            else:
                nodes.update(inspector.nodes)
                binding["dependency_install_ids"] = [
                    node["install_id"]
                    for node in inspector.nodes.values()
                    if node["install_id"] != binding["install_id"]
                ]
        except (CapabilityError, OSError, ValueError, AttributeError):
            unavailable[name] = "dependency_missing"
    for plugin in plugin_ids:
        inspector = _ParentTrackingInspector(context)
        try:
            inspector.visit({"kind": "plugin", "id": plugin}, run.profile or LOCAL_PROFILE)
            if inspector.errors:
                unavailable["plugin:" + plugin] = "dependency_missing"
            else:
                nodes.update(inspector.nodes)
            parents = [
                node["install_id"]
                for node in inspector.nodes.values()
                if node["kind"] in ("plugin", "agent")
            ]
            for node in inspector.nodes.values():
                name = node["install_id"].split(":", 2)[2]
                if node["kind"] == "skill" and name in run.bindings:
                    run.bindings[name].setdefault("dependency_install_ids", []).extend(parents)
                    if inspector.errors:
                        unavailable[name] = "dependency_missing"
        except (CapabilityError, OSError, ValueError):
            unavailable["plugin:" + plugin] = "dependency_missing"
    report = {
        "ready": True,
        "state": "ready",
        "nodes": list(nodes.values()),
        "errors": [],
        "warnings": [],
        "connector_parents": connector_parents,
        "unavailable_skills": [
            {"skill_id": name, "reasons": [{"code": code}]} for name, code in unavailable.items()
        ],
    }
    updated = replace(run, unavailable=unavailable, dependency_report=report)
    save(updated)
    # 这里必须真重建：它顺带是这一轮唯一一次逐个复核冻结字节的机会，
    # 中途被改过的组件要在交给沙箱之前撤下来。不要图快改成按指纹跳过。
    rebuild(updated)
    return get(run.run_id, scope_id=run.scope_id) or updated


_TOOL_SCOPE_PREFIX = "desktop_capability_tool_scope:"


def record_tool_scope(run: PreparedRun, tool_call_id: str, tool_name: str) -> None:
    """Persist adapter ownership before its Intent; collisions never change scope."""
    if not tool_call_id or not tool_name:
        raise IntegrityFailed("tool capability scope requires a durable tool call identity")
    payload = {
        "run_id": run.run_id,
        "user_id": run.user_id,
        "scope_id": run.scope_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
    }
    key = _TOOL_SCOPE_PREFIX + _key(json.dumps([run.run_id, tool_call_id], separators=(",", ":")))
    with _lock, registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            if row.payload != payload:
                raise IntegrityFailed("tool call identity belongs to a different capability scope")
        else:
            db.add(ContentBlock(id=key, payload=payload))


def require_root_tool_scope(run_id: str, user_id: str, tool_call_id: str, tool_name: str) -> None:
    """The legacy recovery adapter can only reconstruct a proven root tool surface."""
    key = _TOOL_SCOPE_PREFIX + _key(json.dumps([run_id, tool_call_id], separators=(",", ":")))
    with registry._session() as db:
        row = db.get(ContentBlock, key)
        if row is not None:
            expected = {
                "run_id": run_id,
                "user_id": user_id,
                "scope_id": "",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
            }
            if row.payload != expected:
                raise IntegrityFailed("scoped tool recovery requires its original child executor")
            return
        for snapshot in db.query(ContentBlock).filter(ContentBlock.id.like(_PREFIX + "%")):
            data = snapshot.payload or {}
            if data.get("run_id") == run_id and data.get("scope_id"):
                raise IntegrityFailed("tool recovery has no proven capability scope")
