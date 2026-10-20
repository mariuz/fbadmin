"""Firebird events over the wire protocol (``op_connect_request`` / ``op_que_events``).

How events work on the wire:

1. The client asks for an *auxiliary connection* with ``op_connect_request`` on its normal
   attachment. The server opens a listening socket (port ``RemoteAuxPort`` in firebird.conf,
   random when 0) and answers with that socket's address.
2. The client opens a second TCP connection to that port. Like fbclient, we connect to the
   address of the main connection's peer and only take the *port* from the answer: the IP the
   server reports is its own (e.g. a Docker container address) and often not reachable.
3. ``op_que_events`` registers interest in a list of names (an "event parameter block": name +
   last known count for each) under a request id chosen by the client. As soon as one of the
   counts on the server is higher than the one sent (after a ``POST_EVENT`` committed), the
   server sends ``op_event`` with the new counts on the auxiliary connection, and that request
   is finished: the client has to queue it again (with the new counts) to keep listening.
4. ``op_cancel_events`` cancels a queued request.

The ``firebirdsql`` driver ships an ``EventConduit`` with this logic, but it connects to the
IP the server reports (broken behind NAT/Docker), handles only one request id per channel,
mis-sizes non-ASCII names and has no way to wait for several requests at once. This module
uses the driver's attachment and packet helpers and does the rest itself.
"""

from __future__ import annotations

import logging
import select
import socket
import threading
import time

from firebirdsql.wireprotocol import Packer

log = logging.getLogger("fbadmin.events")

OP_EXIT = 2
OP_DISCONNECT = 6
OP_QUE_EVENTS = 48
OP_CANCEL_EVENTS = 49
OP_DUMMY = 71
OP_EVENT = 52
OP_CONNECT_REQUEST = 53
EPB_VERSION1 = 1

# isc_event_block() takes at most 15 names; fbclient-based tools split longer lists the same way.
MAX_NAMES_PER_REQUEST = 15
MAX_NAME_BYTES = 255
ANSWER_TIMEOUT = 15.0  # seconds to wait for the server to answer op_connect_request

AUX_HELP = ("Firebird delivers events over a second TCP connection to the port the server sets with "
            "RemoteAuxPort in firebird.conf (random by default). Set RemoteAuxPort to a fixed port, "
            "restart Firebird and open/publish that port in the firewall or Docker "
            "(e.g. -p 3051:3051 -e FIREBIRD_CONF_RemoteAuxPort=3051).")


class AuxConnectionError(ConnectionError):
    """The auxiliary (event) connection could not be opened."""

    def __init__(self, message: str, code: str = "aux_unreachable"):
        super().__init__(message)
        self.code = code


class ChannelClosed(ConnectionError):
    """The server closed the auxiliary connection."""


def encode_name(name: str) -> bytes:
    b = name.encode("utf-8")
    if not b or len(b) > MAX_NAME_BYTES:
        raise ValueError(f"Event names must be 1 to {MAX_NAME_BYTES} bytes long: {name!r}")
    return b


def build_epb(counts: dict[str, int]) -> bytes:
    """Event parameter block: version byte, then (length, name, little-endian count) per name."""
    out = bytearray([EPB_VERSION1])
    for name, n in counts.items():
        b = encode_name(name)
        out += bytes([len(b)]) + b + int(n & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(out)


def parse_epb(data: bytes) -> dict[str, int]:
    if not data or data[0] != EPB_VERSION1:
        raise ValueError("Unrecognized event parameter block")
    out: dict[str, int] = {}
    i = 1
    while i < len(data):
        ln = data[i]
        name = data[i + 1:i + 1 + ln].decode("utf-8", errors="replace")
        out[name] = int.from_bytes(data[i + 1 + ln:i + 5 + ln], "little")
        i += ln + 5
    return out


def parse_sockaddr(buf: bytes) -> tuple[int, str | None]:
    """(port, address) from the server's answer to op_connect_request (a raw sockaddr)."""
    if len(buf) < 4:
        raise AuxConnectionError("The server did not return an address for the event connection. " + AUX_HELP)
    family = int.from_bytes(buf[:2], "little")
    port = int.from_bytes(buf[2:4], "big")
    addr = None
    if family == 2 and len(buf) >= 8:  # AF_INET
        addr = ".".join(str(c) for c in buf[4:8])
    elif family in (10, 23, 30) and len(buf) >= 24:  # AF_INET6 (Linux, Windows, macOS)
        raw = buf[8:24]
        addr = socket.inet_ntop(socket.AF_INET6, raw)
    return port, addr


class EventChannel:
    """One attachment's auxiliary connection plus its queued event requests.

    ``conn`` must be a dedicated ``firebirdsql`` connection: the channel sends packets on it
    (guarded by ``self.lock``; nothing else may use the connection concurrently).
    """

    def __init__(self, conn, *, connect_timeout: float = 5.0, answer_timeout: float | None = None):
        self.conn = conn
        self.lock = threading.RLock()
        self._buf = b""
        self.server_address: str | None = None
        peer = conn.sock._sock.getpeername()[0]
        with self.lock:
            conn._op_connect_request()
            if not _readable(conn, ANSWER_TIMEOUT if answer_timeout is None else answer_timeout):
                # With a fixed RemoteAuxPort the server handles one auxiliary connection request at a
                # time; an earlier one may still be waiting for its client (up to ConnectionTimeout).
                # Our request stays queued on the server: finish it in the background so it does not
                # block the port for another ConnectionTimeout after we gave up.
                threading.Thread(target=_drain, args=(conn, peer), daemon=True).start()
                raise AuxConnectionError(
                    "The server did not answer the request for an event connection in time. Another "
                    "client may be holding the server's auxiliary port (RemoteAuxPort); Firebird frees it "
                    "after ConnectionTimeout (180 s by default). Try again later.", "aux_busy")
            _, _, buf = conn._op_response()
        self.port, self.server_address = parse_sockaddr(buf)
        try:
            self.sock = socket.create_connection((peer, self.port), timeout=connect_timeout)
        except OSError as exc:
            raise AuxConnectionError(
                f"Cannot open the event connection to {peer}:{self.port} ({exc}). "
                f"The server's RemoteAuxPort is not reachable from fbadmin. " + AUX_HELP) from exc
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.sock.settimeout(None)

    # --- requests (main connection) ------------------------------------------------
    def queue(self, rid: int, counts: dict[str, int]) -> None:
        p = Packer()
        p.pack_int(OP_QUE_EVENTS)
        p.pack_int(self.conn.db_handle)
        p.pack_bytes(build_epb(counts))
        p.pack_int(0)  # ast
        p.pack_int(0)  # arg
        p.pack_int(rid)
        with self.lock:
            self.conn.sock.send(p.get_buffer())
            self.conn._op_response()

    def cancel(self, rid: int) -> None:
        p = Packer()
        p.pack_int(OP_CANCEL_EVENTS)
        p.pack_int(self.conn.db_handle)
        p.pack_int(rid)
        with self.lock:
            self.conn.sock.send(p.get_buffer())
            self.conn._op_response()

    # --- notifications (auxiliary connection) ---------------------------------------
    def _recv(self, n: int, timeout: float | None) -> bytes:
        while len(self._buf) < n:
            r, _, _ = select.select([self.sock], [], [], timeout)
            if not r:
                raise TimeoutError
            b = self.sock.recv(max(n - len(self._buf), 4096))
            if not b:
                raise ChannelClosed("The server closed the event connection")
            self._buf += b
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _int(self, timeout: float | None) -> int:
        return int.from_bytes(self._recv(4, timeout), "big", signed=True)

    def read(self, timeout: float | None) -> tuple[int, dict[str, int]] | None:
        """Wait for one notification: (request id, {name: count}); None on timeout."""
        while True:
            try:
                op = self._int(timeout)
            except TimeoutError:
                return None
            # once a packet has started, read the rest of it without giving up halfway
            if op == OP_DUMMY:
                continue
            if op in (OP_EXIT, OP_DISCONNECT):
                raise ChannelClosed("The server closed the event connection")
            if op != OP_EVENT:
                raise ChannelClosed(f"Unexpected packet {op} on the event connection")
            self._int(30)  # database handle
            ln = self._int(30)
            items = self._recv((ln + 3) // 4 * 4, 30)[:ln]
            self._recv(8, 30)  # ast + arg
            rid = self._int(30)
            return rid, parse_epb(items)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _readable(conn, timeout: float) -> bool:
    if getattr(conn.sock, "_recv_buf", b""):  # decompressed data already buffered
        return True
    return bool(select.select([conn.sock._sock], [], [], timeout)[0])


def _drain(conn, peer: str, wait: float = 600) -> None:
    """Complete an abandoned op_connect_request (connect to the port, then drop everything)."""
    try:
        end = time.time() + wait
        while not _readable(conn, 5):
            if time.time() > end:
                return
        _, _, buf = conn._op_response()
        port, _ = parse_sockaddr(buf)
        socket.create_connection((peer, port), timeout=5).close()
        log.info("completed an abandoned event connection request on port %s", port)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.sock.close()
        except Exception:  # noqa: BLE001
            pass
