"""Condition checks, run by the alert engine's checker thread for each enabled condition rule."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..monitoring import probes, replication
from .store import Rule

if TYPE_CHECKING:
    from .engine import Engine


def _meta(server_id: str, db_id: str):
    from ..api.deps import get_state

    return get_state().manager.meta(server_id, db_id)


def _fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def run(engine: Engine, rule: Rule) -> None:
    from .engine import threshold

    limit = threshold(rule)
    sid, dbid = rule.server_id or "", rule.database_id or ""
    try:
        if rule.event == "database.unreachable":
            try:
                with _meta(sid, dbid) as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT 1 FROM RDB$DATABASE")
                    cur.fetchall()
            except Exception as exc:  # noqa: BLE001
                err = str(exc).strip() or exc.__class__.__name__
                engine.set_condition(rule, True, "critical", "Database unreachable",
                                     f"fbadmin cannot connect to the database: {err}", value=err)
                return
            engine.set_condition(rule, False, "info", "Database unreachable", "The database is reachable again.")
            return

        if rule.event in ("oat_gap", "long_transaction"):
            long_running = int(limit or 3600) if rule.event == "long_transaction" else 10 ** 9
            with _meta(sid, dbid) as conn:
                rep = probes.transactions_report(conn, long_running=long_running,
                                                 gap_warn=int(limit or 10000) if rule.event == "oat_gap" else 10 ** 12)
            if rule.event == "oat_gap":
                gap = rep["gaps"]["next_oat"]
                bad = gap >= (limit or 0)
                h = rep["oat_holders"][0] if rep["oat_holders"] else None
                who = (f" The OAT is held by transaction {h['id']} of {h['username']}@{h['remote_address'] or 'local'}"
                       f" ({h['remote_process'] or 'unknown process'}).") if h else ""
                msg = (f"The gap between the next transaction and the oldest active transaction (OAT) is {gap:,} "
                       f"(threshold {int(limit or 0):,}). Old record versions cannot be garbage collected.{who}"
                       if bad else f"The next transaction - OAT gap is back to {gap:,}.")
                level = "critical" if limit and gap >= limit * 10 else "warning"
                engine.set_condition(rule, bad, level, "Transaction gap (OAT) too large", msg, value=gap,
                                     details={"gaps": rep["gaps"], "oat_holder": h and {
                                         k: h.get(k) for k in ("id", "username", "remote_address", "remote_process",
                                                               "age_seconds")}})
            else:
                longs = rep["long_running"]
                oldest = max(longs, key=lambda t: t["age_seconds"] or 0) if longs else None
                bad = oldest is not None
                if oldest:
                    proc = (oldest["remote_process"] or "unknown process").replace("\\", "/").split("/")[-1]
                    msg = (f"{len(longs)} transaction(s) have been running longer than {_fmt_age(limit or 0)}. "
                           f"The oldest, {oldest['id']} ({oldest['username']}@{oldest['remote_address'] or 'local'}, "
                           f"{proc}), started {_fmt_age(oldest['age_seconds'] or 0)} ago.")
                else:
                    msg = f"No transaction is running longer than {_fmt_age(limit or 0)}."
                engine.set_condition(rule, bad, "warning", "Long-running transaction", msg,
                                     value=oldest and oldest["age_seconds"],
                                     details={"transactions": [{k: t.get(k) for k in (
                                         "id", "username", "remote_address", "remote_process", "age_seconds")}
                                         for t in longs[:10]]})
            return

        if rule.event == "replication_lag":
            with _meta(sid, dbid) as conn:
                a = replication.status(conn)
            with _meta(rule.peer_server_id or "", rule.peer_database_id or "") as conn:
                b = replication.status(conn)
            if not a.get("supported") or not b.get("supported"):
                raise ValueError("Replication needs Firebird 4.0 or newer on both sides")
            sa, sb = a.get("replication_sequence"), b.get("replication_sequence")
            if sa is None or sb is None:
                raise ValueError("The replication sequence is not available on both sides")
            primary, replica = (sa, sb) if a.get("replica_mode") == "none" else (sb, sa)
            behind = max(0, primary - replica)
            bad = behind > (limit or 0)
            msg = (f"The replica is {behind} journal segment(s) behind the primary (threshold {int(limit or 0)})."
                   if bad else f"The replica has caught up ({behind} segment(s) behind).")
            engine.set_condition(rule, bad, "warning", "Replication lag", msg, value=behind,
                                 details={"primary_sequence": primary, "replica_sequence": replica})
            return
    except Exception as exc:  # noqa: BLE001 - the check could not run; shown in the rule's status
        engine.check_error(rule, str(exc).strip() or exc.__class__.__name__)
