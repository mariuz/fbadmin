"""Alert delivery: SMTP email, Slack-compatible incoming webhooks and generic JSON webhooks."""

from __future__ import annotations

import hashlib
import hmac
import json
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from .store import Channel, resolve_value

TIMEOUT = 15
LEVEL_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵", "resolved": "🟢"}


def _s(c: Channel, name: str, default: Any = "") -> Any:
    v = c.settings.get(name)
    return resolve_value(v) if v not in (None, "") else default


def subject(alert: dict) -> str:
    return f"[fbadmin] {alert['level'].upper()}: {alert['title']}"


def text_body(alert: dict) -> str:
    lines = [alert["title"], "", alert["message"], ""]
    if alert.get("server"):
        lines.append(f"Server:   {alert['server']}")
    if alert.get("database"):
        lines.append(f"Database: {alert['database']}")
    lines.append(f"Event:    {alert['event']}")
    lines.append(f"Time:     {alert['time_text']}")
    if alert.get("rule"):
        lines.append(f"Rule:     {alert['rule']}")
    return "\n".join(lines) + "\n"


def send_email(c: Channel, alert: dict) -> str:
    host = _s(c, "host")
    security = _s(c, "security", "starttls")
    port = int(_s(c, "port", 0) or (465 if security == "ssl" else 587 if security == "starttls" else 25))
    to = [a.strip() for a in str(_s(c, "to")).replace(";", ",").split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject(alert)
    msg["From"] = _s(c, "from_addr")
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="fbadmin")
    msg.set_content(text_body(alert))
    ctx = ssl.create_default_context()
    smtp: smtplib.SMTP
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    else:
        smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
    with smtp:
        if security == "starttls":
            smtp.starttls(context=ctx)
        user = _s(c, "username")
        if user:
            smtp.login(user, _s(c, "password"))
        smtp.send_message(msg, to_addrs=to)
    return f"sent to {', '.join(to)}"


def _post(url: str, body: bytes, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "fbadmin-alerts",
                                          **headers})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:  # noqa: S310 - admin-configured URL
            return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        detail = e.read(500).decode("utf-8", "replace").strip()
        raise RuntimeError(f"HTTP {e.code}{': ' + detail if detail else ''}") from None


def send_slack(c: Channel, alert: dict) -> str:
    icon = LEVEL_ICON.get(alert["level"], "")
    where = " · ".join(x for x in (alert.get("server"), alert.get("database")) if x)
    text = f"{icon} *{alert['title']}*\n{alert['message']}" + (f"\n_{where}_" if where else "")
    return _post(_s(c, "webhook_url"), json.dumps({"text": text}).encode(), {})


def webhook_payload(alert: dict) -> bytes:
    keys = ("id", "event", "level", "title", "message", "server", "server_id", "database", "database_id",
            "time", "rule", "rule_id", "details")
    return json.dumps({k: alert.get(k) for k in keys}, default=str).encode()


def signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_webhook(c: Channel, alert: dict) -> str:
    body = webhook_payload(alert)
    headers = {"X-Fbadmin-Event": alert["event"]}
    secret = _s(c, "secret")
    if secret:
        headers["X-Fbadmin-Signature"] = signature(secret, body)
    return _post(_s(c, "url"), body, headers)


SENDERS = {"email": send_email, "slack": send_slack, "webhook": send_webhook}


def send(c: Channel, alert: dict) -> str:
    return SENDERS[c.kind](c, alert)
