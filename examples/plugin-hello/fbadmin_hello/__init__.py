"""Example fbadmin plugin."""

from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter

from fbadmin.api.deps import get_state

router = APIRouter(prefix="/api/hello", tags=["hello"])


@router.get("/{server_id}/{db_id}/table-count")
def table_count(server_id: str, db_id: str):
    with get_state().manager.meta(server_id, db_id) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM RDB$RELATIONS WHERE COALESCE(RDB$SYSTEM_FLAG, 0) = 0 AND RDB$VIEW_BLR IS NULL")
        return {"tables": cur.fetchone()[0]}


plugin = SimpleNamespace(
    version="0.1.0",
    router=router,
    static_dir=Path(__file__).parent / "static",
    frontend_entry="hello.js",
)
