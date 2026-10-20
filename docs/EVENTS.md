# Event monitor

Firebird PSQL code can raise named events with `POST_EVENT 'NAME'`. Clients register interest in
names and the server notifies them, with a count per name, when the transaction that posted the
event commits. The event monitor shows those notifications live.

Open it with **Event monitor** in a database's context menu. One monitor tab per database; each
tab has its own connection to the database.

## What the tab shows

- **Registered events**: the names you listen to, with the number of arrivals, the time of the
  last one and a small chart of the last 5 minutes. Type one or more names (separated by commas
  or spaces; put a name with spaces in quotes) and press **Register**. **×** unregisters a name.
- **Posted in PSQL**: names found in `POST_EVENT` calls of triggers, stored procedures, stored
  functions and package bodies, with the objects that post them. Click **+** to register one,
  or **Register all**. Only string literals give a name (`POST_EVENT 'ORDER_PAID'`; `"…"` too,
  which is a string in dialect 1). Calls with a computed name (`POST_EVENT :name`,
  `POST_EVENT 'A' || :x`) are listed below as "posted with a computed name"; register the
  names you expect by hand. **↻** scans the sources again.
- **Post a test event**: runs
  `EXECUTE BLOCK AS BEGIN POST_EVENT 'NAME'; … END` (1 to 100 times) in its own transaction and
  commits it. This is disabled for servers registered as read-only.
- **Live log**: one line per name and notification: time (with milliseconds), name, how many
  times it was posted since the previous notification (`×3`) and the running total. **Reset
  counters** sets all counts to zero and clears the log; **Clear log** only clears the view.

The names you register are remembered in the browser for the next monitor on that database.

### How counting works

- Events are delivered only after the posting transaction **commits**. Events posted in a
  transaction that is rolled back are never delivered.
- If the same name is posted several times before the client is notified (for example three
  times in one transaction), the count goes up by that number in one notification.
- Counts start when a name is registered; posts that happened before are not counted.

## Server requirement: the auxiliary port (RemoteAuxPort)

Firebird does not send events over the normal connection. The client asks the server for an
*auxiliary connection*: the server opens a listening port and the client connects to it. The
port is set by `RemoteAuxPort` in `firebird.conf`; the default `0` means a random port each time.

That random port works on a local network without a firewall, but not when:

- Firebird runs in Docker and only 3050 is published,
- a firewall only allows port 3050,
- the server is reached through NAT or a port forward.

Then the monitor shows **Cannot open the event connection … The server's RemoteAuxPort is not
reachable from fbadmin** and nothing is registered. To fix it:

1. Set a fixed port in `firebird.conf` and restart Firebird:

   ```
   RemoteAuxPort = 3051
   ```

2. Allow that port in the firewall (TCP, from the fbadmin host to the server).

With the official Docker image, set it with an environment variable and publish the port
**with the same number** on both sides (the server tells the client its own port number):

```bash
docker run -d --name firebird -p 3050:3050 -p 3051:3051 \
  -e FIREBIRD_ROOT_PASSWORD=masterkey -e FIREBIRD_CONF_RemoteAuxPort=3051 \
  firebirdsql/firebird:5
```

fbadmin connects to the auxiliary port on the same host address as the main connection (like
fbclient does); it does not use the IP address the server reports, which is often an internal
container address.

### "The server's event port is busy"

With a fixed `RemoteAuxPort` the server can set up only one auxiliary connection at a time. If a
client asks for one and never connects (for example because the port is blocked), the server
keeps the port for `ConnectionTimeout` seconds (180 by default) and other requests wait. fbadmin
gives up after 15 seconds with **The server's event port is busy** and finishes its own pending
request in the background, so it does not hold the port any longer than needed. Wait and press
**Try again**.

## Sessions and limits

- Each monitor tab opens one attachment to the database (visible in the Processes view) and one
  auxiliary connection. It is closed when the tab is closed.
- A monitor that is not polled for 5 minutes (browser closed) is closed by the server. The tab
  then shows **expired**; **Reconnect** opens it again with the same names. The same button
  appears when the connection is lost (server restart, attachment killed).
- At most 500 names per monitor and 20 monitors per fbadmin process. Names are registered in
  requests of 15 (the limit of fbclient's `isc_event_block`); more names simply use more
  requests. Event names are at most 255 bytes (UTF-8).
- Environment variables: `FBADMIN_EVENTS_IDLE_TIMEOUT` (seconds, default 300) and
  `FBADMIN_EVENTS_MAX_SESSIONS` (default 20).
- On a server registered as read-only you can listen to events, but not post them.

## REST API

All under `/api/servers/{server_id}/databases/{db_id}/event-monitor`:

| Method and path | Body | Result |
|---|---|---|
| `GET /suggestions` | | `{names: [{name, sources: [{type, name, parent}]}], dynamic: [{type, name, parent, expression}]}` |
| `POST /sessions` | `{names: [...]}` | opens a monitor; `{session, log}` (502 with `code` `aux_unreachable` or `aux_busy` when the auxiliary connection fails) |
| `GET /sessions/{id}?after=N` | | `{session, log}`: counts per name and the log entries with `seq > N` |
| `POST /sessions/{id}/register` | `{names: [...]}` | adds names; `added` lists the new ones |
| `POST /sessions/{id}/unregister` | `{names: [...]}` | removes names |
| `POST /sessions/{id}/reset` | | zeroes the counters and clears the log |
| `POST /sessions/{id}/reconnect` | | reopens an expired or failed monitor |
| `DELETE /sessions/{id}` | | closes it |
| `POST /post` | `{name, count}` | posts an event `count` times in one committed transaction |

## How it works (for contributors)

`backend/fbadmin/events/wire.py` speaks the event part of the wire protocol on top of the
`firebirdsql` driver's attachment: `op_connect_request` (answer: the server's aux socket
address), a plain TCP connection to that port, `op_que_events` with an event parameter block
(names and last known counts) under a request id, `op_event` notifications on the auxiliary
connection, and `op_cancel_events`. The driver has its own `EventConduit`, but it connects to the
IP address the server reports, handles one request per connection and miscounts non-ASCII
names, so fbadmin does not use it.

`backend/fbadmin/events/monitor.py` keeps one session per tab. The first notification after
queueing a request carries the server's current counts and is only used as the baseline. Every
notification finishes its request, so the session queues the same names again with the new
counts; the difference to the previous counts is the number of arrivals. The browser polls the
session once a second (every 5 seconds while the tab is hidden), like the trace viewer.

`backend/fbadmin/events/scan.py` finds `POST_EVENT` calls in `RDB$TRIGGERS`,
`RDB$PROCEDURES`, `RDB$FUNCTIONS` and `RDB$PACKAGES` sources, skipping comments and other
strings.

### Tests

The live tests need a server with a reachable auxiliary port.
`scripts/events-test-server.sh [3|4|5|6 …]` starts containers with a fixed, published
`RemoteAuxPort` (Firebird 5 on 3060/3061, 3 on 3062/3063, 4 on 3064/3065, 6 on 3066/3067) and
prints the variable to export:

```bash
scripts/events-test-server.sh 5
cd backend && FBADMIN_TEST_EVENTS_PORT=3060 ../.venv/bin/python -m pytest -q tests/test_events.py
```

Without `FBADMIN_TEST_EVENTS_PORT` those tests are skipped. `test_unreachable_aux_port` uses the
normal test server (`FBADMIN_TEST_PORT`), whose random auxiliary port is not published. The
Playwright spec `e2e/tests/events.spec.ts` uses `FB_EVENTS_PORT` the same way.
