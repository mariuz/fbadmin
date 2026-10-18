from types import SimpleNamespace

from fastapi import APIRouter


def test_data_dir_and_entry_point_plugins(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from fbadmin import config, plugins
    from fbadmin.api import deps

    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "hello.js").write_text("export default function (fb) { fb.registry.registerTopbar({id:'x', render: () => null}); }")

    router = APIRouter()

    @router.get("/api/hello-plugin")
    def hello():
        return {"hi": True}

    static = tmp_path / "pkgstatic"
    static.mkdir()
    (static / "main.js").write_text("export default () => {}")
    called = {}
    obj = SimpleNamespace(router=router, static_dir=static, frontend_entry="main.js", version="1.2",
                          setup=lambda app: called.setdefault("setup", True))
    ep = SimpleNamespace(name="demo", load=lambda: obj)
    bad = SimpleNamespace(name="broken", load=lambda: (_ for _ in ()).throw(ImportError("boom")))
    monkeypatch.setattr(plugins, "_entry_points", lambda: [ep, bad])
    deps.reset_state()
    from fbadmin.app import create_app

    with TestClient(create_app()) as c:
        lst = {p["name"]: p for p in c.get("/api/plugins").json()}
        assert lst["demo"]["frontend"] == "/plugins/demo/main.js" and lst["demo"]["version"] == "1.2"
        assert "boom" in lst["broken"]["error"]
        assert lst["hello"]["frontend"] == "/plugins/local/hello.js"
        assert c.get("/plugins/local/hello.js").status_code == 200
        assert c.get("/plugins/demo/main.js").status_code == 200
        assert c.get("/api/hello-plugin").json() == {"hi": True}
    assert called["setup"]
