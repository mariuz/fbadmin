"""Low-level Services API helpers.

The ``firebirdsql`` driver implements only a few service actions and reads
``isc_info_svc_line`` lengths as one byte. These helpers build raw SPBs and run
the query loop ourselves so every action (nbackup, validation, limbo, streaming
backup/restore, new-style shutdown, trace) is available.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable, Iterator

from .. import jobs

# --- actions -----------------------------------------------------------------
ACT_BACKUP = 1
ACT_RESTORE = 2
ACT_REPAIR = 3
ACT_PROPERTIES = 8
ACT_DB_STATS = 11
ACT_NBAK = 20
ACT_NREST = 21
ACT_TRACE_START = 22
ACT_TRACE_STOP = 23
ACT_TRACE_SUSPEND = 24
ACT_TRACE_RESUME = 25
ACT_TRACE_LIST = 26
ACT_VALIDATE = 30
ACT_NFIX = 31  # Firebird 4+

# --- generic SPB items ---------------------------------------------------------
SPB_COMMAND_LINE = 105
SPB_DBNAME = 106
SPB_VERBOSE = 107
SPB_OPTIONS = 108

# --- info items ------------------------------------------------------------------
INFO_END = 1
INFO_TRUNCATED = 2
INFO_DATA_NOT_READY = 4
INFO_SVC_LINE = 62
INFO_SVC_TO_EOF = 63
INFO_SVC_TIMEOUT = 64
INFO_SVC_LIMBO_TRANS = 66
INFO_SVC_RUNNING = 67
INFO_SVC_STDIN = 78

# --- backup / restore -------------------------------------------------------------
BKP_FILE = 5
BKP_FACTOR = 6
BKP_LENGTH = 7
BKP_SKIP_DATA = 8
BKP_STAT = 15
BKP_KEYHOLDER = 16
BKP_KEYNAME = 17
BKP_CRYPT = 18
BKP_INCLUDE_DATA = 19
BKP_PARALLEL_WORKERS = 21
BKP_OPT_IGNORE_CHECKSUMS = 0x01
BKP_OPT_IGNORE_LIMBO = 0x02
BKP_OPT_METADATA_ONLY = 0x04
BKP_OPT_NO_GARBAGE_COLLECT = 0x08
BKP_OPT_OLD_DESCRIPTIONS = 0x10
BKP_OPT_NON_TRANSPORTABLE = 0x20
BKP_OPT_CONVERT = 0x40
BKP_OPT_EXPAND = 0x80
BKP_OPT_NO_TRIGGERS = 0x8000
BKP_OPT_ZIP = 0x010000

RES_BUFFERS = 9
RES_PAGE_SIZE = 10
RES_ACCESS_MODE = 12
RES_FIX_FSS_DATA = 13
RES_FIX_FSS_METADATA = 14
RES_OPT_METADATA_ONLY = 0x04
RES_OPT_DEACTIVATE_IDX = 0x0100
RES_OPT_NO_SHADOW = 0x0200
RES_OPT_NO_VALIDITY = 0x0400
RES_OPT_ONE_AT_A_TIME = 0x0800
RES_OPT_REPLACE = 0x1000
RES_OPT_CREATE = 0x2000
RES_OPT_USE_ALL_SPACE = 0x4000

# --- properties ----------------------------------------------------------------------
PRP_PAGE_BUFFERS = 5
PRP_SWEEP_INTERVAL = 6
PRP_SHUTDOWN_DB = 7
PRP_DENY_NEW_ATTACHMENTS = 9
PRP_DENY_NEW_TRANSACTIONS = 10
PRP_RESERVE_SPACE = 11
PRP_WRITE_MODE = 12
PRP_ACCESS_MODE = 13
PRP_SET_SQL_DIALECT = 14
PRP_OPT_ACTIVATE = 0x0100
PRP_OPT_DB_ONLINE = 0x0200
PRP_OPT_NOLINGER = 0x0400
PRP_FORCE_SHUTDOWN = 41
PRP_ATTACHMENTS_SHUTDOWN = 42
PRP_TRANSACTIONS_SHUTDOWN = 43
PRP_SHUTDOWN_MODE = 44
PRP_ONLINE_MODE = 45
PRP_RES_USE_FULL = 35
PRP_RES = 36
PRP_WM_ASYNC = 37
PRP_WM_SYNC = 38
PRP_AM_READONLY = 39
PRP_AM_READWRITE = 40
SHUT_MODES = {"normal": 0, "multi": 1, "single": 2, "full": 3}

# --- repair ---------------------------------------------------------------------------
RPR_COMMIT_TRANS = 15
RPR_RECOVER_TWO_PHASE = 17
RPR_ROLLBACK_TRANS = 34
RPR_COMMIT_TRANS_64 = 49
RPR_ROLLBACK_TRANS_64 = 50
RPR_RECOVER_TWO_PHASE_64 = 51
RPR_PAR_WORKERS = 52
RPR_OPT_VALIDATE_DB = 0x01
RPR_OPT_SWEEP_DB = 0x02
RPR_OPT_MEND_DB = 0x04
RPR_OPT_LIST_LIMBO = 0x08
RPR_OPT_CHECK_DB = 0x10
RPR_OPT_IGNORE_CHECKSUM = 0x20
RPR_OPT_KILL_SHADOWS = 0x40
RPR_OPT_FULL = 0x80
RPR_OPT_ICU = 0x0800

TRA_ID = 18
SINGLE_TRA_ID = 19
MULTI_TRA_ID = 20
TRA_STATE = 21
TRA_STATE_NAMES = {22: "limbo", 23: "commit", 24: "rollback", 25: "unknown"}
TRA_HOST_SITE = 26
TRA_REMOTE_SITE = 27
TRA_DB_PATH = 28
TRA_ADVISE = 29
TRA_ADVISE_NAMES = {30: "commit", 31: "rollback", 33: "unknown"}
TRA_ID_64 = 46
SINGLE_TRA_ID_64 = 47
MULTI_TRA_ID_64 = 48

# --- online validation -----------------------------------------------------------------
VAL_TAB_INCL = 1
VAL_TAB_EXCL = 2
VAL_IDX_INCL = 3
VAL_IDX_EXCL = 4
VAL_LOCK_TIMEOUT = 5

# --- nbackup -------------------------------------------------------------------------
NBK_LEVEL = 5
NBK_FILE = 6
NBK_DIRECT = 7
NBK_GUID = 8
NBK_OPT_NO_TRIGGERS = 0x01
NBK_OPT_INPLACE = 0x02
NBK_OPT_SEQUENCE = 0x04  # nbackup -SEQ: keep the replication sequence (Firebird 4.0.1+)

# --- statistics ------------------------------------------------------------------------
STS_DATA_PAGES = 0x01
STS_HDR_PAGES = 0x04
STS_IDX_PAGES = 0x08
STS_SYS_RELATIONS = 0x10
STS_RECORD_VERSIONS = 0x20
STS_TABLE = 0x40  # with SPB_COMMAND_LINE listing table names

# --- trace -----------------------------------------------------------------------------
TRC_ID = 1
TRC_NAME = 2
TRC_CFG = 3


class Spb:
    """Service parameter buffer builder (little-endian, 2-byte string lengths)."""

    def __init__(self, action: int):
        self.buf = bytearray([action])

    def str(self, tag: int, value: str) -> Spb:
        b = value.encode("utf-8")
        self.buf += bytes([tag]) + struct.pack("<H", len(b)) + b
        return self

    def int(self, tag: int, value: int) -> Spb:
        self.buf += bytes([tag]) + struct.pack("<I", int(value) & 0xFFFFFFFF)
        return self

    def int64(self, tag: int, value: int) -> Spb:
        self.buf += bytes([tag]) + struct.pack("<Q", int(value))
        return self

    def byte(self, tag: int, value: int) -> Spb:
        self.buf += bytes([tag, value])
        return self

    def flag(self, tag: int) -> Spb:
        self.buf += bytes([tag])
        return self

    def raw(self) -> bytes:
        return bytes(self.buf)


def start(svc, spb: Spb | bytes) -> None:
    jobs.check()  # never start a service action for a cancelled job
    svc._op_service_start(spb.raw() if isinstance(spb, Spb) else spb)
    h, _oid, _buf = svc._op_response()
    svc.svc_handle = h


def query(svc, items: bytes, send: bytes = b"", buflen: int = 65535) -> bytes:
    svc._op_service_info(send, items, buflen)
    _h, _oid, buf = svc._op_response()
    return buf


def timeout_send(seconds: int) -> bytes:
    return bytes([INFO_SVC_TIMEOUT]) + struct.pack("<H", 4) + struct.pack("<I", seconds) + bytes([INFO_END])


def _u16(b: bytes, i: int) -> int:
    return struct.unpack_from("<H", b, i)[0]


def read_line(svc, timeout: int | None = None) -> tuple[str | None, bool]:
    """Read one output line. Returns (line, finished); line is None when nothing arrived in time."""
    buf = query(svc, bytes([INFO_SVC_LINE]), timeout_send(timeout) if timeout else b"")
    if not buf or buf[0] != INFO_SVC_LINE:
        return None, buf[:1] == bytes([INFO_END])
    n = _u16(buf, 1)
    rest = buf[3 + n:4 + n]
    if n:
        return buf[3:3 + n].decode("utf-8", "replace"), False
    if rest in (bytes([INFO_SVC_TIMEOUT]), bytes([INFO_DATA_NOT_READY]), bytes([INFO_TRUNCATED])):
        return None, False
    return None, True


def drain(svc, callback: Callable[[str], None] | None = None, timeout: int = 5,
          stop: Callable[[], bool] | None = None) -> list[str]:
    """Read text output of the running service until it finishes.

    In a job, stops with ``jobs.Cancelled`` when the job is cancelled (checked after each line,
    and at least every ``timeout`` seconds while the service is quiet)."""
    lines: list[str] = []
    while True:
        if jobs.cancelling():
            stop_cancelled(lambda: read_line(svc, 1)[1])
        line, finished = read_line(svc, timeout)
        if finished:
            return lines
        if line is not None:
            lines.append(line)
            if callback:
                callback(line)
        if stop and stop():
            return lines


def run(svc, spb: Spb | bytes, callback: Callable[[str], None] | None = None, timeout: int = 5) -> list[str]:
    """Start a service action and collect its text output (calling callback per line)."""
    start(svc, spb)
    return drain(svc, callback, timeout)


def iter_to_eof(svc) -> Iterator[bytes]:
    """Yield raw output (e.g. a gbak backup written to stdout) until the service finishes."""
    while True:
        if jobs.cancelling():
            stop_cancelled(lambda: _eof_chunk(svc, 1)[1])
        buf = query(svc, bytes([INFO_SVC_TO_EOF]), timeout_send(30))
        i = 0
        tag = None
        n = 0
        while i < len(buf):
            tag = buf[i]
            if tag != INFO_SVC_TO_EOF:
                break
            n = _u16(buf, i + 1)
            if n:
                yield bytes(buf[i + 3:i + 3 + n])
            i += 3 + n
        if tag == INFO_END:
            return


def _eof_chunk(svc, timeout: int) -> tuple[bytes, bool]:
    """One ``isc_info_svc_to_eof`` read: (data, finished)."""
    buf = query(svc, bytes([INFO_SVC_TO_EOF]), timeout_send(timeout))
    data, i, tag = b"", 0, None
    while i < len(buf):
        tag = buf[i]
        if tag != INFO_SVC_TO_EOF:
            break
        n = _u16(buf, i + 1)
        data += bytes(buf[i + 3:i + 3 + n])
        i += 3 + n
    return data, tag == INFO_END


def stop_cancelled(read_finished: Callable[[], bool], wait: float = 30) -> None:
    """The job was cancelled while a service action runs: detach the action's attachment (the
    job's interrupt hooks; repeated, because the action may only just have attached), read until
    the service ends, then raise ``jobs.Cancelled``. Without hooks there is nothing that could stop
    the action on the server, so this raises at once."""
    if jobs.interrupt():
        deadline = time.time() + wait
        last = time.time()
        while time.time() < deadline:
            try:
                if read_finished():
                    break
            except Exception:  # noqa: BLE001 - the detached action ends the service with an error
                break
            if time.time() - last >= 0.5:
                jobs.interrupt()
                last = time.time()
    raise jobs.Cancelled()


def feed_stdin(svc, read: Callable[[int], bytes], callback: Callable[[str], None] | None = None,
               verbose: bool = True) -> None:
    """Drive a service that reads ``stdin`` (gbak restore from stdin), sending data when asked."""
    request_len = 0
    while True:
        jobs.check()  # a cancelled restore stops reading; detaching then ends gbak (see jobs.py)
        send = b""
        if request_len > 0:
            data = read(min(request_len, 32000))
            send = bytes([INFO_SVC_LINE]) + struct.pack("<H", len(data)) + data + bytes([INFO_END])
        items = bytes([INFO_SVC_LINE, INFO_SVC_STDIN]) if verbose else bytes([INFO_SVC_STDIN])
        buf = query(svc, items, send)
        i = 0
        request_len = 0
        got_line = False
        not_ready = False
        while i < len(buf) and buf[i] != INFO_END:
            tag = buf[i]
            if tag == INFO_SVC_STDIN:
                request_len = struct.unpack_from("<I", buf, i + 1)[0]
                i += 5
            elif tag == INFO_SVC_LINE:
                n = _u16(buf, i + 1)
                if n:
                    got_line = True
                    if callback:
                        callback(buf[i + 3:i + 3 + n].decode("utf-8", "replace"))
                i += 3 + n
            elif tag in (INFO_DATA_NOT_READY, INFO_SVC_TIMEOUT, INFO_TRUNCATED):
                not_ready = True
                i += 1
            else:
                raise RuntimeError(f"Unexpected service response tag {tag}")
        if not (got_line or not_ready or request_len):
            return


def parse_limbo(buf: bytes) -> list[dict]:
    """Parse an isc_info_svc_limbo_trans response into a list of transactions."""
    out: list[dict] = []
    cur: dict | None = None
    i = 0
    while i < len(buf):
        tag = buf[i]
        if tag in (INFO_END, INFO_TRUNCATED):
            break
        if tag == INFO_SVC_LIMBO_TRANS:
            i += 3
            continue
        i += 1
        if tag in (SINGLE_TRA_ID, MULTI_TRA_ID, TRA_ID):
            v = struct.unpack_from("<I", buf, i)[0]
            i += 4
        elif tag in (SINGLE_TRA_ID_64, MULTI_TRA_ID_64, TRA_ID_64):
            v = struct.unpack_from("<Q", buf, i)[0]
            i += 8
        elif tag in (TRA_STATE, TRA_ADVISE):
            v = buf[i]
            i += 1
        elif tag in (TRA_HOST_SITE, TRA_REMOTE_SITE, TRA_DB_PATH):
            n = _u16(buf, i)
            v = buf[i + 2:i + 2 + n].decode("utf-8", "replace")
            i += 2 + n
        else:
            break
        if tag in (SINGLE_TRA_ID, MULTI_TRA_ID, SINGLE_TRA_ID_64, MULTI_TRA_ID_64):
            cur = {"id": v, "multi": tag in (MULTI_TRA_ID, MULTI_TRA_ID_64), "state": None, "advise": None,
                   "host_site": None, "remote_site": None, "db_path": None, "remote_id": None}
            out.append(cur)
        elif cur is None:
            continue
        elif tag in (TRA_ID, TRA_ID_64):
            cur["remote_id"] = v
        elif tag == TRA_STATE:
            cur["state"] = TRA_STATE_NAMES.get(v, str(v))
        elif tag == TRA_ADVISE:
            cur["advise"] = TRA_ADVISE_NAMES.get(v, str(v))
        elif tag == TRA_HOST_SITE:
            cur["host_site"] = v
        elif tag == TRA_REMOTE_SITE:
            cur["remote_site"] = v
        elif tag == TRA_DB_PATH:
            cur["db_path"] = v
    return out
