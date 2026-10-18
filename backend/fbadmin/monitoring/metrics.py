"""Prometheus text exposition (format 0.0.4) of MON$ snapshots of the registered databases."""

from __future__ import annotations

from typing import Any

# name, help, type, snapshot key
METRICS: list[tuple[str, str, str, str]] = [
    ("firebird_attachments", "User attachments (connections) to the database.", "gauge", "attachments"),
    ("firebird_transactions", "Transactions currently active in the database.", "gauge", "transactions"),
    ("firebird_statements_active", "Statements currently executing.", "gauge", "active_statements"),
    ("firebird_statements_stalled", "Statements with an open cursor waiting for a fetch.", "gauge",
     "stalled_statements"),
    ("firebird_next_transaction", "Next transaction number.", "counter", "next_transaction"),
    ("firebird_oldest_transaction", "Oldest interesting transaction (OIT).", "gauge", "oit"),
    ("firebird_oldest_active", "Oldest active transaction (OAT).", "gauge", "oat"),
    ("firebird_oldest_snapshot", "Oldest snapshot transaction (OST).", "gauge", "ost"),
    ("firebird_page_reads", "Pages read from disk since the database was opened.", "counter", "page_reads"),
    ("firebird_page_writes", "Pages written to disk since the database was opened.", "counter", "page_writes"),
    ("firebird_page_fetches", "Pages fetched from the cache since the database was opened.", "counter",
     "page_fetches"),
    ("firebird_page_marks", "Pages marked dirty since the database was opened.", "counter", "page_marks"),
    ("firebird_record_seq_reads", "Records read by full table scans.", "counter", "seq_reads"),
    ("firebird_record_idx_reads", "Records read via an index.", "counter", "idx_reads"),
    ("firebird_record_inserts", "Records inserted.", "counter", "inserts"),
    ("firebird_record_updates", "Records updated.", "counter", "updates"),
    ("firebird_record_deletes", "Records deleted.", "counter", "deletes"),
    ("firebird_record_backouts", "Record versions backed out.", "counter", "backouts"),
    ("firebird_record_purges", "Record versions purged by garbage collection.", "counter", "purges"),
    ("firebird_record_expunges", "Records expunged by garbage collection.", "counter", "expunges"),
    ("firebird_record_waits", "Record lock waits.", "counter", "record_waits"),
    ("firebird_record_conflicts", "Record update conflicts.", "counter", "record_conflicts"),
    ("firebird_memory_used_bytes", "Memory used by the database.", "gauge", "memory_used"),
    ("firebird_memory_allocated_bytes", "Memory allocated from the OS for the database.", "gauge",
     "memory_allocated"),
]


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(d: dict[str, str]) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in d.items()) + "}"


def render(samples: list[tuple[dict[str, str], dict[str, Any] | None, float]]) -> str:
    """``samples``: [(labels, snapshot or None if the scrape failed, scrape seconds)]."""
    out: list[str] = [
        "# HELP fbadmin_database_up 1 if the database could be scraped.",
        "# TYPE fbadmin_database_up gauge",
    ]
    for labels, snap, _ in samples:
        out.append(f"fbadmin_database_up{_labels(labels)} {1 if snap else 0}")
    out += ["# HELP fbadmin_scrape_duration_seconds Time spent reading MON$ tables.",
            "# TYPE fbadmin_scrape_duration_seconds gauge"]
    for labels, _snap, secs in samples:
        out.append(f"fbadmin_scrape_duration_seconds{_labels(labels)} {secs:.6f}")
    for name, help_, mtype, key in METRICS:
        full = name + ("_total" if mtype == "counter" else "")
        out.append(f"# HELP {full} {help_}")
        out.append(f"# TYPE {full} {mtype}")
        for labels, snap, _ in samples:
            if snap and snap.get(key) is not None:
                out.append(f"{full}{_labels(labels)} {snap[key]}")
    return "\n".join(out) + "\n"
