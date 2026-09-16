#!/usr/bin/env python3
"""Community-edition site publishing MCP server."""

from __future__ import annotations

from typing import Any, Dict, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp_servers.site_publish_mcp import impl

mcp = FastMCP("hugagent-site-publish")

_HDR_USER = "x-current-user-id"
_HDR_CHAT = "x-chat-id"
_HDR_CONV = "x-conversation-id"


def _hdr(ctx: Optional[Context], name: str) -> Optional[str]:
    if ctx is None:
        return None
    try:
        value = ctx.request_context.request.headers.get(name)
        return value or None
    except Exception:
        return None


def _identity(ctx: Optional[Context]) -> Dict[str, str]:
    """Identity/session come from the runtime headers agent_factory injects."""
    return {
        "user_id": _hdr(ctx, _HDR_USER) or "",
        "chat_id": _hdr(ctx, _HDR_CHAT) or _hdr(ctx, _HDR_CONV) or "",
    }


@mcp.tool()
async def list_sites(ctx: Context | None = None) -> Dict[str, Any]:
    """List the account's published sites the caller may edit, with their source folders.

    Call this before changing any published site — from a site card, the site's
    project chat, or a brand-new conversation. The returned ``site_id`` is what
    ``publish_site`` needs to update the original site; publishing without it
    creates a new site and leaves the user's existing URL untouched.

    Each item carries ``site_id``, ``title``, ``url``, ``version``, ``kind``
    (``static`` or ``build``), ``source_dir`` (where to edit), ``publish_dir``
    (``src_dir`` for static sites; empty for build sites, which must be rebuilt),
    ``in_current_project`` and ``editable``.

    With one candidate, use it. With several and no clear request, ask the user —
    never default to the most recently published one. An error or an empty list
    does not mean the user has no sites, so never create a new site because of it.
    """
    return await impl.list_sites(**_identity(ctx))


@mcp.tool()
async def publish_site(
    title: str,
    src_dir: str = "",
    source_dir: str = "",
    slug: str = "",
    site_id: str = "",
    visibility: str = "public",
    description: str = "",
    ctx: Context | None = None,
) -> Dict[str, Any]:
    """Publish a site from the current sandbox and return its hosted URL.

    ``visibility`` accepts ``public`` or ``private``. Static sites may omit
    ``src_dir`` in a project chat. Build-based sites pass the build output as
    ``src_dir`` and the editable source folder as ``source_dir``.

    To update a published site, call ``list_sites`` first and pass that
    ``site_id`` back here; omitting it creates a separate new site.
    """
    return await impl.publish_site(
        **_identity(ctx),
        src_dir=src_dir,
        source_dir=source_dir,
        title=title,
        slug=slug,
        site_id=site_id,
        visibility=visibility,
        description=description,
    )


# ── Site KV (the data plane of a published site's built-in light backend) ──
#
# In-site JS writes this through __api/kv. An agent in the sandbox holds no site
# session cookie, so curling __api/kv 404s on private/team sites — it reads and
# writes through these four tools instead.


def _kv_target(ctx: Context | None, site_id: str, slug: str) -> Dict[str, Any]:
    """KV is authorized by site ownership and never touches the sandbox — no chat id."""
    return {
        "user_id": _hdr(ctx, _HDR_USER) or "",
        "site_id": site_id,
        "slug": slug,
    }


@mcp.tool()
async def site_kv_list(
    site_id: str = "", slug: str = "", limit: int = 50, ctx: Context | None = None
) -> Dict[str, Any]:
    """列出站点 KV 里现有的键，看站内 JS（__api/kv）写进来的数据。

    站点编号和访问地址传其一即可：site_id 取自 list_sites 或 publish_site 回执，
    slug 是访问地址 /site/<slug>/ 中间那段。

    值只给前 200 字预览，value_chars 是全长；要全文用 site_kv_get。
    total 大于返回条数说明还有更多，调大 limit 再取。

    Args:
        site_id (`str`, 可选): 站点编号。
        slug (`str`, 可选): 站点访问路径；不传 site_id 时必传。
        limit (`int`, 可选): 本次返回条数，默认 50，上限 200。

    Returns:
        JSON: {ok, site_id, slug, total, items:[{key, preview, value_chars,
        updated_at}]} 或 {error: '...'}。
    """
    return await impl.site_kv(action="list", limit=limit, **_kv_target(ctx, site_id, slug))


@mcp.tool()
async def site_kv_get(
    key: str, site_id: str = "", slug: str = "", ctx: Context | None = None
) -> Dict[str, Any]:
    """读取站点 KV 中某个键的完整值。

    Args:
        key (`str`): 键名（字母/数字/`_.:-`，≤64 位）。
        site_id (`str`, 可选): 站点编号。
        slug (`str`, 可选): 站点访问路径；不传 site_id 时必传。

    Returns:
        JSON: {ok, site_id, key, value, exists} 或 {error: '...'}。
    """
    return await impl.site_kv(action="get", key=key, **_kv_target(ctx, site_id, slug))


@mcp.tool()
async def site_kv_set(
    key: str, value: str, site_id: str = "", slug: str = "", ctx: Context | None = None
) -> Dict[str, Any]:
    """写入/覆盖站点 KV 中的一个键，改的就是站点线上正在读的那份数据。

    KV 是站点自带的轻量数据库，改数据是日常操作、随时可做，**不需要重新发布站点**。

    判断该改 KV 还是该重新发布，看改的是数据还是代码：页面某处显示的值、而该处
    本来就从 KV 读 → 改 KV 即时生效；页面的版式/结构/交互/新增栏目 → 改源码走
    publish_site。值目前写死在源码里、而用户会反复改它 → 先改造成从 KV 读并发布
    一次，之后只动 KV。接到"把 X 改成 Y"先读源码确认 X 是写死的还是 kvGet 来的。

    结构化内容自己先 JSON 序列化成字符串再传。
    覆盖是整值替换，改之前先用 site_kv_get 看现值，不要凭印象重写。

    限制：单值 ≤4KB，每站 ≤200 个键。

    Args:
        key (`str`): 键名（字母/数字/`_.:-`，≤64 位）。
        value (`str`): 新值（字符串；对象/数组先 JSON 序列化）。
        site_id (`str`, 可选): 站点编号。
        slug (`str`, 可选): 站点访问路径；不传 site_id 时必传。

    Returns:
        JSON: {ok, site_id, key} 或 {error: '...'}。
    """
    return await impl.site_kv(action="set", key=key, value=value, **_kv_target(ctx, site_id, slug))


@mcp.tool()
async def site_kv_delete(
    key: str, site_id: str = "", slug: str = "", ctx: Context | None = None
) -> Dict[str, Any]:
    """删除站点 KV 中某个键。删除不可撤销，执行前先跟用户确认要删哪个键。

    Args:
        key (`str`): 键名。
        site_id (`str`, 可选): 站点编号。
        slug (`str`, 可选): 站点访问路径；不传 site_id 时必传。

    Returns:
        JSON: {ok, site_id, key, deleted} 或 {error: '...'}。
    """
    return await impl.site_kv(action="delete", key=key, **_kv_target(ctx, site_id, slug))


def main() -> None:
    from mcp_servers import _serve

    _serve.run(mcp, default_port=9113)


if __name__ == "__main__":
    main()
