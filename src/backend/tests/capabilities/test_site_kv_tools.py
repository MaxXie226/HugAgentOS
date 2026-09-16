"""智能体侧的站点 KV 通道：站内 JS 走 __api/kv，沙箱里的智能体走这条内部接口。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def kv_client(tmp_path, monkeypatch):
    from api.routes.v1 import internal_sites
    from core.db.engine import Base
    from core.db.models import UserShadow
    from core.services.site_service import SiteService
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'kv.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setenv("STORAGE_TYPE", "local")
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path / "storage"))
    monkeypatch.setenv("BACKEND_INTERNAL_TOKEN", "t0ken")
    monkeypatch.setattr(
        "core.services.desktop_cloud_bridge.bridge_enabled", lambda: False, raising=False
    )

    with factory() as db:
        db.add(UserShadow(user_id="owner", username="owner"))
        db.add(UserShadow(user_id="stranger", username="stranger"))
        db.commit()
        site = SiteService(db).publish(
            user_id="owner", files=[("index.html", b"<h1>kv</h1>")], title="KV 站", slug="kv-site"
        )
        site_id = site.site_id

    app = FastAPI()
    app.include_router(internal_sites.router)
    client = TestClient(app)

    def post(body, token="t0ken"):
        return client.post("/v1/internal/sites/kv", json=body, headers={"X-Internal-Token": token})

    def call(action, user_id="owner", **kw):
        resp = post({"action": action, "user_id": user_id, **kw})
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]

    yield call, post, site_id
    engine.dispose()


def test_agent_can_read_and_write_kv_by_site_id(kv_client):
    call, _post, site_id = kv_client
    assert call("get", site_id=site_id, key="score")["exists"] is False

    assert call("set", site_id=site_id, key="score", value="42")["ok"] is True
    assert call("get", site_id=site_id, key="score")["value"] == "42"

    listed = call("list", site_id=site_id)
    assert listed["total"] == 1
    assert listed["items"][0]["key"] == "score"
    assert listed["items"][0]["preview"] == "42"

    assert call("delete", site_id=site_id, key="score")["deleted"] is True
    assert call("list", site_id=site_id)["total"] == 0


def test_slug_locates_the_same_site(kv_client):
    call, _post, site_id = kv_client
    call("set", slug="kv-site", key="motto", value="hello")
    assert call("get", site_id=site_id, key="motto")["value"] == "hello"
    # 站点地址带斜杠照抄进来也认
    assert call("get", slug="/kv-site/", key="motto")["value"] == "hello"


def test_long_values_are_previewed_not_dumped(kv_client):
    call, _post, site_id = kv_client
    call("set", site_id=site_id, key="doc", value="x" * 1000)
    item = call("list", site_id=site_id)["items"][0]
    assert item["value_chars"] == 1000 and len(item["preview"]) == 200
    # 全文只在显式 get 时回灌
    assert call("get", site_id=site_id, key="doc")["value"] == "x" * 1000


def test_list_is_capped_and_total_signals_more(kv_client):
    """200 个键全量回灌会撑爆上下文；只回一页，靠 total 告诉模型还有更多。"""
    call, _post, site_id = kv_client
    for i in range(5):
        call("set", site_id=site_id, key=f"k{i}", value=str(i))
    page = call("list", site_id=site_id, limit=2)
    assert len(page["items"]) == 2 and page["total"] == 5
    assert len(call("list", site_id=site_id)["items"]) == 5


def test_service_limits_and_missing_site_surface_as_readable_errors(kv_client):
    call, _post, site_id = kv_client
    assert "error" in call("set", site_id=site_id, key="bad key!", value="x")
    assert "error" in call("set", site_id=site_id, key="big", value="x" * 5000)
    assert "error" in call("list")  # 既没 site_id 也没 slug
    assert "error" in call("get", slug="no-such-site", key="k")


def test_unknown_action_is_rejected_before_touching_the_site(kv_client):
    """桥接分支要拿 action 拼云端工具名，未校验的串不能放过去。"""
    call, _post, site_id = kv_client
    assert "error" in call("nonsense", site_id=site_id)


def test_other_users_cannot_touch_the_site(kv_client):
    call, _post, site_id = kv_client
    assert "error" in call("set", user_id="stranger", site_id=site_id, key="k", value="v")


def test_internal_token_is_enforced(kv_client):
    _call, post, site_id = kv_client
    resp = post({"action": "list", "user_id": "owner", "site_id": site_id}, token="wrong")
    assert resp.status_code == 401


def test_kv_permission_levels_are_declared_once(kv_client):
    """读写级别只在 site_service 声明一次，管理台与智能体两条路都读它。"""
    import inspect

    from api.routes.v1 import sites as sites_route
    from core.services.site_service import KV_READ_LEVEL, KV_WRITE_LEVEL

    assert (KV_READ_LEVEL, KV_WRITE_LEVEL) == ("view", "edit")
    panel = inspect.getsource(sites_route)
    for fn in ("list_site_kv", "delete_site_kv", "clear_site_kv"):
        assert fn in panel
    assert "required=KV_READ_LEVEL" in panel and "required=KV_WRITE_LEVEL" in panel


def test_plugin_manifest_ships_the_kv_tools():
    """工具没进 plugin.json 就不会暴露给模型——装/升级链路以它为准。"""
    import json
    import pathlib

    manifest = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[2]
            / "plugin_bundles/marketplace/sites/plugin.json"
        ).read_text()
    )
    tools = manifest["extensions"]["org.hugagent"]["mcp"]["site_publish"]["tools"]
    names = {t["name"] for t in tools}
    assert {"site_kv_list", "site_kv_get", "site_kv_set", "site_kv_delete"} <= names


async def test_mcp_server_exposes_the_kv_tools_with_usable_schemas():
    """参数 schema 丢了的工具会被模型空参数乱调——注册表里必须带全参数。"""
    from mcp_servers.site_publish_mcp import server

    schemas = {
        t.name: (t.inputSchema or {}).get("properties", {}) for t in await server.mcp.list_tools()
    }
    assert set(schemas["site_kv_list"]) == {"site_id", "slug", "limit"}
    assert set(schemas["site_kv_get"]) == {"key", "site_id", "slug"}
    assert set(schemas["site_kv_set"]) == {"key", "value", "site_id", "slug"}
    assert set(schemas["site_kv_delete"]) == {"key", "site_id", "slug"}
