"""SQL notebooks (roadmap 2.3): the document store - cells, results, charts, conflicts, revisions, export and
import - and sharing in server mode, where saved results follow the viewer's database permissions.

These tests do not need a Firebird server (cells run through the Query Tool's sessions, tested elsewhere;
the e2e test runs a notebook against a real server).
"""

from __future__ import annotations

import pytest

from fbadmin import notebooks as N

from .test_library import _switch, desktop, srv  # noqa: F401 - fixtures
from .test_security import ADMIN_PW, login

RESULT = {"kind": "select", "message": "2 row(s) fetched", "row_count": 2,
          "columns": [{"name": "REGION", "type": "VARCHAR"}, {"name": "TOTAL", "type": "BIGINT"}],
          "rows": [["north", 10], ["south | east", 7]]}


def _cells(output=True):
    return [
        {"id": "n1", "type": "markdown", "source": "## Month-end close\nCheck the totals.", "output": {"error": "x"}},
        {"id": "q1", "type": "sql", "source": "SELECT REGION, SUM(AMOUNT) AS TOTAL FROM SALES GROUP BY 1",
         "chart": {"type": "bar", "x": 0, "ys": [1]},
         "output": {"ran_by": "me", "elapsed_ms": 4.2, "results": [RESULT]} if output else None},
    ]


def test_crud_conflicts_revisions_export(desktop):  # noqa: F811
    c = desktop
    assert c.get("/api/notebooks").json() == {"server_mode": False, "user": None, "notebooks": []}
    nb = c.post("/api/notebooks", json={"name": "  Close   checks ", "visibility": "shared"}).json()
    assert nb["name"] == "Close checks" and nb["visibility"] == "private"  # desktop: nothing to share with
    assert [x["type"] for x in nb["cells"]] == ["sql"] and nb["rev"] == 1 and nb["can_edit"]

    r = c.put(f"/api/notebooks/{nb['id']}", json={"base_rev": 1, "cells": _cells(), "description": "monthly"})
    assert r.status_code == 200, r.text
    nb = r.json()
    assert nb["rev"] == 2 and nb["cell_count"] == 2
    note, q = nb["cells"]
    assert note["output"] is None  # notes have no output
    assert q["chart"] == {"type": "bar", "x": 0, "ys": [1], "group": None, "agg": "sum", "sort": "auto", "topN": None}
    assert q["output"]["results"][0]["rows"] == [["north", 10], ["south | east", 7]]

    # a save based on an older revision is refused with the current notebook
    r = c.put(f"/api/notebooks/{nb['id']}", json={"base_rev": 1, "name": "stale"})
    assert r.status_code == 409 and "revision 2" in r.json()["detail"] and r.json()["notebook"]["rev"] == 2
    # invalid content
    assert c.put(f"/api/notebooks/{nb['id']}", json={"cells": [{"id": "a"}, {"id": "a"}]}).status_code == 400
    bad_chart = {"id": "b", "chart": {"type": "bar", "x": 0, "ys": list(range(9))}}
    assert c.put(f"/api/notebooks/{nb['id']}", json={"cells": [bad_chart]}).status_code == 422
    assert c.put(f"/api/notebooks/{nb['id']}", json={"name": " "}).status_code in (400, 422)

    # quick saves by the same person update one revision; results alone make none
    for i in range(3):
        cells = _cells()
        cells[1]["source"] += f" -- v{i}"
        nb = c.put(f"/api/notebooks/{nb['id']}", json={"cells": cells}).json()
    cells = nb["cells"]
    cells[1]["output"]["results"][0]["rows"] = [["west", 1]]
    nb = c.put(f"/api/notebooks/{nb['id']}", json={"cells": cells}).json()
    assert nb["rev"] == 6
    revs = c.get(f"/api/notebooks/{nb['id']}/revisions").json()["revisions"]
    assert [(r["rev"], r["cells"]) for r in revs] == [(5, 2), (1, 1)]  # saves 2-5 merged, 6 changed results only
    old = c.get(f"/api/notebooks/{nb['id']}/revisions/5").json()
    assert old["cells"][1]["source"].endswith("-- v2") and old["cells"][1]["output"] is None
    nb = c.post(f"/api/notebooks/{nb['id']}/revisions/1/restore").json()
    assert [x["source"] for x in nb["cells"]] == [""] and nb["rev"] == 7
    assert c.get(f"/api/notebooks/{nb['id']}/revisions/3").status_code == 404

    # export: JSON (round-trips through import) and Markdown
    cells = _cells()
    nb = c.put(f"/api/notebooks/{nb['id']}", json={"cells": cells}).json()
    doc = c.get(f"/api/notebooks/{nb['id']}/export").json()
    assert doc["format"] == "fbadmin-notebook" and doc["cells"][1]["output"]["results"][0]["rows"][0] == ["north", 10]
    assert c.get(f"/api/notebooks/{nb['id']}/export", params={"results": "false"}).json()["cells"][1]["output"] is None
    md = c.get(f"/api/notebooks/{nb['id']}/export", params={"format": "md"})
    assert md.headers["content-type"].startswith("text/markdown")
    assert 'filename="Close checks.md"' in md.headers["content-disposition"]
    text = md.text
    assert text.startswith("# Close checks\n\n## Month-end close")  # the restore emptied the description
    assert "```sql\nSELECT REGION" in text and "| REGION | TOTAL |" in text and "| south \\| east | 7 |" in text
    assert "*Chart: bar of TOTAL by REGION*" in text and "by me" in text
    imp = c.post("/api/notebooks/import", json={"notebook": doc}).json()
    assert imp["name"] == "Close checks" and imp["id"] != nb["id"]
    assert imp["cells"][1]["output"]["results"][0]["rows"][1] == ["south | east", 7]
    assert {x["id"] for x in imp["cells"]}.isdisjoint({"n1", "q1"})
    assert c.post("/api/notebooks/import", json={"notebook": {"cells": []}}).status_code == 400

    # list and search
    assert {n["name"] for n in c.get("/api/notebooks").json()["notebooks"]} == {"Close checks"}
    assert len(c.get("/api/notebooks", params={"q": "close GROUP"}).json()["notebooks"]) == 2
    assert c.get("/api/notebooks", params={"q": "nothing-like-this"}).json()["notebooks"] == []
    listed = c.get("/api/notebooks").json()["notebooks"][0]
    assert "cells" not in listed and listed["cell_count"] == 2

    # copy and delete
    cp = c.post(f"/api/notebooks/{nb['id']}/copy").json()
    assert cp["name"] == "Close checks (copy)" and cp["cells"][1]["output"] is not None
    assert c.delete(f"/api/notebooks/{cp['id']}").status_code == 204
    assert c.get(f"/api/notebooks/{cp['id']}").status_code == 404

    # the store survives a restart
    N._store = None
    assert c.get(f"/api/notebooks/{nb['id']}").json()["cells"][1]["chart"]["ys"] == [1]


def test_limits(desktop, monkeypatch):  # noqa: F811
    c = desktop
    nb = c.post("/api/notebooks", json={"name": "big"}).json()
    many = {**RESULT, "rows": [[str(i), i] for i in range(N.MAX_ROWS + 50)]}
    cells = [{"id": "q", "source": "x", "output": {"results": [many] * (N.MAX_RESULTS + 2)}}]
    nb = c.put(f"/api/notebooks/{nb['id']}", json={"cells": cells}).json()
    out = nb["cells"][0]["output"]["results"]
    assert len(out) == N.MAX_RESULTS and len(out[0]["rows"]) == N.MAX_ROWS
    monkeypatch.setattr(N, "MAX_BYTES", 2000)
    r = c.put(f"/api/notebooks/{nb['id']}", json={"cells": cells + [{"id": "q2", "source": "y"}]})
    assert r.status_code == 400 and "too large" in r.json()["detail"]


def test_server_mode_sharing_and_result_permissions(srv):  # noqa: F811
    c = srv
    login(c, "admin@example.org", ADMIN_PW)
    for name in ("alice", "dave"):
        assert c.post("/api/admin/users", json={"username": name, "password": f"{name}-password",
                                                "role": "user"}).status_code == 201
    s1 = c.post("/api/servers", json={"name": "prod", "host": "h1", "shared": True}).json()
    sales = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "sales", "path": "/db/sales.fdb"}).json()
    hr = c.post(f"/api/servers/{s1['id']}/databases", json={"name": "hr", "path": "/db/hr.fdb"}).json()
    # dave may read sales only; alice has no grants (everything)
    r = c.post("/api/admin/access/grants", json={"subject_type": "user", "subject": "dave", "server_id": s1["id"],
                                                  "db_id": sales["id"], "level": "read"})
    assert r.status_code == 201, r.text

    _switch(c, "alice", "alice-password")
    on_hr = c.post("/api/notebooks", json={"name": "salaries", "server_id": s1["id"], "database_id": hr["id"],
                                           "visibility": "shared", "cells": _cells()})
    assert on_hr.status_code == 201, on_hr.text
    on_hr = on_hr.json()
    on_sales = c.post("/api/notebooks", json={"name": "sales by region", "server_id": s1["id"],
                                              "database_id": sales["id"], "visibility": "shared",
                                              "cells": _cells()}).json()
    private = c.post("/api/notebooks", json={"name": "scratch"}).json()
    assert on_hr["owner"] == "alice" and on_hr["cells"][1]["output"] is not None

    _switch(c, "dave", "dave-password")
    names = {n["name"] for n in c.get("/api/notebooks").json()["notebooks"]}
    assert names == {"salaries", "sales by region"}
    assert c.get(f"/api/notebooks/{private['id']}").status_code == 404
    # hr results are hidden from dave; sales results are shown
    h = c.get(f"/api/notebooks/{on_hr['id']}").json()
    assert h["results_hidden"] is True and h["cells"][1]["output"] is None and h["cells"][1]["source"]
    assert h["can_edit"] is False
    assert c.get(f"/api/notebooks/{on_hr['id']}/export").json()["cells"][1]["output"] is None
    assert "| north |" not in c.get(f"/api/notebooks/{on_hr['id']}/export", params={"format": "md"}).text
    s = c.get(f"/api/notebooks/{on_sales['id']}").json()
    assert s["results_hidden"] is False and s["cells"][1]["output"]["results"][0]["rows"][0] == ["north", 10]
    # dave cannot change alice's shared notebooks, but can copy them (without the hidden results)
    r = c.put(f"/api/notebooks/{on_sales['id']}", json={"name": "mine now"})
    assert r.status_code == 403
    assert c.delete(f"/api/notebooks/{on_sales['id']}").status_code == 403
    cp = c.post(f"/api/notebooks/{on_hr['id']}/copy").json()
    assert cp["owner"] == "dave" and cp["visibility"] == "private" and cp["cells"][1]["output"] is None
    own = c.post("/api/notebooks", json={"name": "dave's", "server_id": s1["id"], "database_id": sales["id"]})
    assert own.status_code == 201  # a restricted user may keep notebooks

    # an administrator may change shared notebooks; saving without the hidden results keeps them
    _switch(c, "admin@example.org", ADMIN_PW)
    a = c.get(f"/api/notebooks/{on_hr['id']}").json()
    assert a["results_hidden"] is False
    r = c.put(f"/api/notebooks/{on_hr['id']}", json={"base_rev": a["rev"], "description": "by admin"})
    assert r.status_code == 200 and r.json()["updated_by"] == "admin@example.org" and r.json()["owner"] == "alice"
    assert c.put(f"/api/notebooks/{on_hr['id']}", json={"visibility": "private"}).status_code == 400
    assert c.get(f"/api/notebooks/{private['id']}").status_code == 404

    # the owner makes it private: dave no longer sees it
    _switch(c, "alice", "alice-password")
    assert c.put(f"/api/notebooks/{on_hr['id']}", json={"visibility": "private"}).status_code == 200
    _switch(c, "dave", "dave-password")
    assert c.get(f"/api/notebooks/{on_hr['id']}").status_code == 404


def test_hidden_results_are_kept_when_a_restricted_editor_saves():
    """A user who cannot see the results saves the notebook: unchanged SQL cells keep their saved results."""
    from fbadmin.library import Actor

    st = N.NotebookStore.__new__(N.NotebookStore)
    st.items, st._lock = {}, __import__("threading").RLock()
    st._save = lambda nb: None
    owner = Actor(user_id=1, username="alice", is_admin=False)
    nb = st.create({"name": "x", "server_id": "s", "database_id": "d", "cells": _cells()}, owner)
    import fbadmin.notebooks as mod

    orig = mod.results_allowed
    mod.results_allowed = lambda _nb: False
    try:
        stripped = [N.Cell(**{**cell.model_dump(), "output": None}) for cell in nb.cells]
        stripped.append(N.Cell(id="new", source="SELECT 1"))
        new = st.update(nb.id, {"cells": stripped, "name": "y"}, owner)
    finally:
        mod.results_allowed = orig
    assert new.cells[1].output is not None and new.cells[1].output.results[0].rows[0] == ["north", 10]
    assert new.cells[2].output is None


@pytest.mark.parametrize("bad", ["ABC", "x" * 40, "..x"])
def test_ids_are_checked(desktop, bad):  # noqa: F811
    assert desktop.get(f"/api/notebooks/{bad}").status_code == 404
