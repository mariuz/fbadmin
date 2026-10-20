"""API routers. Feature modules add themselves to ROUTERS (one line each, to merge cleanly)."""

from fastapi import APIRouter

from . import ai  # noqa: I001 - one import per line keeps parallel branches mergeable
from . import databases
from . import jobs
from . import maintenance
from . import query
from . import schedules
from . import servers
from . import trace
from . import metrics
from . import monitoring
from . import replication
from . import version as version_api
from . import object_dialogs
from . import schema
from . import admin
from . import auth
from . import fbsecurity
from . import storage
from . import alerts
from . import datacompare
from . import embedded
from . import upgrade
from . import events
from . import debug
from . import querybuilder

ROUTERS: list[APIRouter] = [
    servers.router,
    databases.router,
    query.router,
    jobs.router,
    ai.router,
    maintenance.router,
    trace.router,
    schedules.router,
    monitoring.router,
    monitoring.settings_router,
    metrics.router,
    replication.router,
    replication.conf_router,
    version_api.router,
    object_dialogs.router,
    schema.router,
    auth.router,
    admin.router,
    fbsecurity.router,
    storage.router,
    alerts.router,
    datacompare.router,
    embedded.router,
    upgrade.router,
    events.router,
    debug.router,
    querybuilder.router,
]
