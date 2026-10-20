"""Alerts API: channels (email, Slack, webhook), rules, history. Administrators only in server mode."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from ..alerts import checks
from ..alerts.engine import get_engine
from ..alerts.store import CONDITIONS, EVENTS, Channel, Rule, get_store
from ..security import runas
from ..security.core import require_admin
from ..serialize import to_json

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


class ChannelIn(BaseModel):
    name: str
    kind: Literal["email", "slack", "webhook"]
    enabled: bool = True
    settings: dict[str, Any] = {}


class ChannelUpdate(BaseModel):
    name: str | None = None
    kind: Literal["email", "slack", "webhook"] | None = None
    enabled: bool | None = None
    settings: dict[str, Any] | None = None


class RuleIn(BaseModel):
    name: str
    event: str
    enabled: bool = True
    server_id: str | None = None
    database_id: str | None = None
    threshold: float | None = None
    peer_server_id: str | None = None
    peer_database_id: str | None = None
    channels: list[str] = []
    cooldown_minutes: int = 60
    notify_resolved: bool = True


class RuleUpdate(BaseModel):
    name: str | None = None
    event: str | None = None
    enabled: bool | None = None
    server_id: str | None = None
    database_id: str | None = None
    threshold: float | None = None
    peer_server_id: str | None = None
    peer_database_id: str | None = None
    channels: list[str] | None = None
    cooldown_minutes: int | None = None
    notify_resolved: bool | None = None


def _rule_view(r: Rule) -> dict:
    d = r.model_dump()
    ra = d.pop("run_as", None)
    d["run_as_user"] = ra.get("user") if ra else None
    d["status"] = get_engine().status.get(r.id) if r.event in CONDITIONS else None
    return to_json(d)


@router.get("/events")
def events():
    require_admin()
    return EVENTS


# ---------------------------------------------------------------- channels
@router.get("/channels")
def list_channels():
    require_admin()
    return get_store().list_channels()


@router.post("/channels", status_code=201)
def create_channel(body: ChannelIn):
    require_admin()
    st = get_store()
    return st.view_channel(st.add_channel(Channel(**body.model_dump())))


@router.put("/channels/{channel_id}")
def update_channel(channel_id: str, body: ChannelUpdate):
    require_admin()
    st = get_store()
    return st.view_channel(st.update_channel(channel_id, body.model_dump(exclude_unset=True)))


@router.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: str):
    require_admin()
    get_store().remove_channel(channel_id)


@router.post("/channels/{channel_id}/test")
def test_channel(channel_id: str):
    """Send a test message now; the error is returned rather than raised so the UI can show it."""
    require_admin()
    try:
        return get_engine().send_test(channel_id)
    except KeyError:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc) or exc.__class__.__name__}


# ------------------------------------------------------------------- rules
@router.get("/rules")
def list_rules():
    require_admin()
    return [_rule_view(r) for r in sorted(get_store().rules.values(), key=lambda r: r.created_at)]


@router.post("/rules", status_code=201)
def create_rule(body: RuleIn):
    require_admin()
    data = body.model_dump()
    if data["event"] in CONDITIONS and data.get("server_id"):
        data["run_as"] = runas.issue([data["server_id"], data.get("peer_server_id")], f"alert rule: {body.name}")
    return _rule_view(get_store().add_rule(Rule(**data)))


@router.put("/rules/{rule_id}")
def update_rule(rule_id: str, body: RuleUpdate):
    require_admin()
    st = get_store()
    cur = st.get_rule(rule_id)
    changes = body.model_dump(exclude_unset=True)
    merged = {**cur.model_dump(), **changes}
    cred = None
    if merged["event"] in CONDITIONS and merged.get("server_id"):
        cred = runas.issue([merged["server_id"], merged.get("peer_server_id")], f"alert rule: {merged['name']}")
        if cred is not None:
            changes["run_as"] = cred
    rule = st.update_rule(rule_id, changes)
    if cred is not None:
        runas.revoke(cur.run_as)
    return _rule_view(rule)


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: str):
    require_admin()
    st = get_store()
    old = st.get_rule(rule_id).run_as
    st.remove_rule(rule_id)
    runas.revoke(old)
    get_engine().status.pop(rule_id, None)


@router.post("/rules/{rule_id}/check")
def check_rule(rule_id: str):
    """Run a condition rule's check now (alerts are sent as usual)."""
    require_admin()
    rule = get_store().get_rule(rule_id)
    if rule.event not in CONDITIONS:
        raise ValueError("Only condition rules can be checked; event rules fire when the event happens")
    checks.run(get_engine(), rule)
    return _rule_view(rule)


# ----------------------------------------------------------------- history
@router.get("/history")
def history(limit: int = 100):
    require_admin()
    return to_json(get_engine().recent(max(1, min(limit, 500))))


@router.delete("/history", status_code=204)
def clear_history():
    require_admin()
    get_engine().clear_history()
