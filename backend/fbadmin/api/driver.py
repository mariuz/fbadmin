"""Server-wide "Database driver" setting: pure-Python ``firebirdsql`` or native ``firebird-driver``.

``GET /api/driver`` reports the selection, where it comes from (environment, saved setting or
default) and whether the native driver can be used (package, client library and its version).
``PUT /api/driver`` saves a new selection. Both are for administrators only in server mode (in
desktop mode the local user is the administrator). See fbadmin/drivers.py and docs/DRIVERS.md.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from .. import drivers
from ..security.core import require_admin
from .deps import get_state

router = APIRouter(prefix="/api/driver", tags=["driver"])


class DriverIn(BaseModel):
    driver: str | None = None  # "firebirdsql" or "native"; None keeps the current one
    fbclient: str | None = None  # client library path; "" clears it; None keeps it


@router.get("")
def get_driver():
    require_admin()
    return drivers.status()


@router.put("")
def put_driver(body: DriverIn):
    """Save the driver choice. Pooled metadata connections are closed so new ones use the selected
    driver; open Query Tool sessions keep their connection (and driver) until they are closed."""
    require_admin()
    before_driver = drivers.selected()[0]
    before = dict(drivers.stored())
    if body.driver is not None:
        new = drivers.normalize(body.driver)
        if drivers.selected()[1] == "environment" and new != before_driver:
            raise ValueError(f"The driver is set by the environment ({drivers.ENV_DRIVER}); "
                             "change it there and restart fbadmin.")
    if body.fbclient is not None and drivers.fbclient_setting()[1] == "environment" \
            and body.fbclient.strip() != (drivers.fbclient_setting()[0] or ""):
        raise ValueError(f"The client library is set by the environment ({drivers.ENV_FBCLIENT}); "
                         "change it there and restart fbadmin.")
    drivers.save(driver=body.driver, fbclient=body.fbclient)
    if drivers.selected()[0] == "native":
        nat = drivers.native_status()
        if not nat["available"]:
            drivers.restore(before)
            raise ValueError(f"The native driver cannot be used: {nat['reason']}")
    closed = 0
    if drivers.selected()[0] != before_driver:
        closed = get_state().manager.close_pools()
    return {**drivers.status(), "closed_connections": closed}
