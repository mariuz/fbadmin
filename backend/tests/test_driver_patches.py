"""Binary parameters and OCTETS columns round-trip (driver patches in fbadmin/driver_patches.py)."""

import uuid

import firebirdsql

import fbadmin  # noqa: F401 - applies the driver patches

from .conftest import DB_DIR, HOST, PASSWORD, PORT, USER, requires_server


@requires_server
def test_octets_roundtrip():
    c = firebirdsql.create_database(host=HOST, port=PORT, user=USER, password=PASSWORD,
                                    database=f"{DB_DIR}/fbadmin_octets_{uuid.uuid4().hex[:8]}.fdb")
    try:
        cur = c.cursor()
        cur.execute("create table b (id int, d blob sub_type binary, u char(16) character set octets, "
                    "v varchar(4) character set octets, t varchar(20))")
        c.commit()
        u = uuid.uuid4().bytes
        cur.execute("insert into b values (?, ?, ?, ?, ?)", [1, b"\x00\xff", u, b"\xff\x00", "héllo ü"])
        c.commit()
        cur.execute("select d, u, v, t from b where u = ? and t = ?", [u, "héllo ü"])
        assert cur.fetchall() == [(b"\x00\xff", u, b"\xff\x00", "héllo ü")]
    finally:
        c.drop_database()
