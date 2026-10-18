"""Database encryption end to end, with a real crypt plugin.

Needs a Firebird 4+/5 server with the sample crypt plugins that ship in every official Linux
release tarball (examples/prebuilt/plugins: libfbSampleDbCrypt.so, libfbSampleKeyHolder.so), both
configured with ``Auto = true`` and ``KeyHolderPlugin = fbSampleKeyHolder`` in firebird.conf.
``scripts/test-servers.sh`` starts one as ``fb5crypt`` on port 3055. Point the test at it with
``FBADMIN_TEST_CRYPT_PORT=3055``; without it the test is skipped.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from .conftest import DB_DIR, HOST, PASSWORD, USER

CRYPT_PORT = os.environ.get("FBADMIN_TEST_CRYPT_PORT")
pytestmark = pytest.mark.skipif(not CRYPT_PORT, reason="FBADMIN_TEST_CRYPT_PORT not set (server with a crypt plugin)")
PLUGIN = os.environ.get("FBADMIN_TEST_CRYPT_PLUGIN", "fbSampleDbCrypt")


def _wait(client, base, want: str, timeout=60):
    st = None
    for _ in range(timeout * 4):
        st = client.get(base + "/crypt-state").json()
        if st["crypt_state_name"] == want:
            return st
        time.sleep(0.25)
    raise AssertionError(f"crypt state never became {want!r}: {st}")


def test_encrypt_and_decrypt_with_a_real_plugin(client):
    srv = client.post("/api/servers", json={"name": "crypt", "host": HOST, "port": int(CRYPT_PORT),
                                            "user": USER, "password": PASSWORD}).json()
    path = f"{DB_DIR}/fbadmin_crypt_{uuid.uuid4().hex[:8]}.fdb"
    db = client.post(f"/api/servers/{srv['id']}/databases/create", json={"name": "c", "path": path}).json()
    dbase = f"/api/servers/{srv['id']}/databases/{db['id']}"
    m = dbase + "/maintenance"
    try:
        r = client.post(dbase + "/execute", json={"sql": "CREATE TABLE SECRET (ID INTEGER, S VARCHAR(40));\n"
                                                         "INSERT INTO SECRET VALUES (1, 'top secret');"})
        assert r.json()["error"] is None
        assert client.get(m + "/crypt-state").json()["crypt_state_name"] == "not encrypted"

        r = client.post(m + "/encrypt", json={"plugin": PLUGIN})
        assert r.status_code == 200, r.text
        st = _wait(client, m, "encrypted")
        assert st["progress"] is not None
        # the header page (via the Services API) and MON$DATABASE agree
        props = client.get(m + "/properties").json()
        assert props["header"]["encrypted"] is True, props["header"]["attributes"]
        assert props["database"]["crypt_state_name"] == "encrypted"
        # data is still readable through the key holder
        q = client.post(dbase + "/execute", json={"sql": "SELECT S FROM SECRET"}).json()
        assert q["results"][0]["rows"] == [["top secret"]]
        # encrypting twice is refused by the engine
        assert client.post(m + "/encrypt", json={"plugin": PLUGIN}).status_code == 400

        r = client.post(m + "/decrypt")
        assert r.status_code == 200, r.text
        _wait(client, m, "not encrypted")
        assert client.get(m + "/properties").json()["header"]["encrypted"] is False
    finally:
        client.post(dbase + "/disconnect")
        client.post(dbase + "/drop")
