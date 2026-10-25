"""Work-around for wire compression in the ``firebirdsql`` driver.

The driver switches the socket to zlib as soon as it has read the first 16 bytes
of the server's accept packet, but Firebird sends the *whole* accept packet
(including the ``op_cond_accept`` authentication data) uncompressed and only
compresses what follows. Connecting with ``wire_compress=True`` therefore fails
with "zlib.error: unknown compression method".

The patch defers compression until the client sends its next packet, which is
always after the accept packet has been read completely.

Second problem: with a connection ``timeout`` the driver calls ``select()`` on the
raw socket before every read, but with compression the bytes may already be
waiting, decompressed, in the stream's buffer - ``select()`` then times out
("Pakcet recv timeout error"). The patched ``_recv_channel`` skips ``select()``
while buffered data is available. firebirdsql 1.5 reads rows ahead into the
connection's ``_recv_buf``; the patched ``_recv_channel`` reads that first, as the
driver's own does (otherwise an error after a fetch, such as a cancelled query,
blocks forever). Its read-ahead fetch loop has the ``select()`` problem too and
gets the same guard (``_patch_fetch_select``).

Both patches are idempotent and only change behaviour for compressed connections.
"""

from __future__ import annotations

import inspect
import logging
import select
import textwrap

from firebirdsql import fbcore as _fbcore
from firebirdsql import stream as _stream
from firebirdsql.err import OperationalError

log = logging.getLogger(__name__)

_PATCHED = "_fbadmin_deferred_compression"


def apply() -> None:
    cls = _stream.SocketStream
    if getattr(cls, _PATCHED, False):
        return
    orig_enable = cls.enable_compression
    orig_send = cls.send

    def enable_compression(self):
        self._fbadmin_compress_pending = True

    def send(self, b):
        if getattr(self, "_fbadmin_compress_pending", False):
            self._fbadmin_compress_pending = False
            orig_enable(self)
        return orig_send(self, b)

    cls.enable_compression = enable_compression
    cls.send = send
    setattr(cls, _PATCHED, True)

    def _recv_channel(self, nbytes, word_alignment=False):
        n = nbytes
        if word_alignment and (n % 4):
            n += 4 - nbytes % 4  # 4 bytes word alignment
        r = bytearray()
        rbuf = getattr(self, "_recv_buf", None)  # firebirdsql 1.5+: bytes read ahead while fetching
        if rbuf:
            take = min(n, len(rbuf))
            r += rbuf[:take]
            del rbuf[:take]
            n -= take
        while n:
            buffered = getattr(self.sock, "_decompressor", None) is not None and bool(self.sock._recv_buf)
            if (self.timeout is not None and not buffered
                    and select.select([self.sock._sock], [], [], self.timeout)[0] == []):
                raise TimeoutError("Pakcet recv timeout error")
            b = self.sock.recv(n)
            if not b:
                break
            r += b
            n -= len(b)
        if len(r) < nbytes:
            raise OperationalError("Can not recv() packets")
        return bytes(r[:nbytes])

    _fbcore.ConnectionResponseMixin._recv_channel = _recv_channel
    _patch_fetch_select()


_FETCH_SELECT = "if self.timeout is not None and select.select([self.sock._sock], [], [], self.timeout)[0] == []:"


def _patch_fetch_select() -> None:
    """firebirdsql 1.5: skip ``select()`` in ``_op_fetch_response`` while decompressed bytes are buffered."""
    try:
        fn = _fbcore.ConnectionResponseMixin._op_fetch_response
        if getattr(fn, "_fbadmin", False):
            return
        src = textwrap.dedent(inspect.getsource(fn))
        if _FETCH_SELECT not in src:
            return  # older driver (no read-ahead) or fixed upstream
        src = src.replace(_FETCH_SELECT, (
            "if (self.timeout is not None"
            " and not (getattr(self.sock, '_decompressor', None) is not None and self.sock._recv_buf)"
            " and select.select([self.sock._sock], [], [], self.timeout)[0] == []):"))
        ns: dict = {}
        exec(compile(src, "<fbadmin wirecompat patch>", "exec"), _fbcore.__dict__, ns)
        patched = ns["_op_fetch_response"]
        patched._fbadmin = True
        _fbcore.ConnectionResponseMixin._op_fetch_response = patched
    except Exception:  # noqa: BLE001
        log.exception("could not patch firebirdsql _op_fetch_response for wire compression")
