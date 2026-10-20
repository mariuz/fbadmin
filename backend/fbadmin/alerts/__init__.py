"""Alerts (milestone 1.1): notify by email, Slack or webhook when something needs attention.

* ``store``    – channels and rules, persisted in ``<data dir>/alerts.json``;
* ``channels`` – delivery (SMTP, Slack-compatible webhooks, generic JSON webhooks);
* ``engine``   – ``emit()`` for events raised by fbadmin (failed scheduled jobs, failed backup
  verification, validation errors, failed uploads) and a checker thread for conditions polled
  from the databases (OAT gap, long-running transactions, replication lag, unreachable database).
"""

from .engine import emit  # noqa: F401
