"""corlet.std -- optional standard components built on the core.

Nothing here is started unless you add it to a container::

    from corlet.std import HealthComponent, SchedulerComponent
    Container(*my_components, SchedulerComponent, HealthComponent)

or from the CLI: ``corlet --std scheduler,health ...``.
"""

from .admin import ADMIN_ROUTES, AdminComponent, AdminConfig, AdminServer, Request, Response, Route
from .config import ConfigService, ConfigServiceComponent, ConfigWatcher, LayeredConfig
from .health import (
    HEALTH_CHECKS,
    CheckResult,
    HealthCheck,
    HealthComponent,
    HealthReport,
    HealthService,
    Status,
)
from .logging import ComponentFilter, JsonFormatter, LoggingComponent, configure_logging
from .reload import ReloadComponent
from .scheduler import JOBS, Job, JobHandle, Scheduler, SchedulerComponent

STD_COMPONENTS: dict[str, type] = {
    "config": ConfigServiceComponent,
    "config-watcher": ConfigWatcher,
    "logging": LoggingComponent,
    "scheduler": SchedulerComponent,
    "health": HealthComponent,
    "admin": AdminComponent,
    "reload": ReloadComponent,
}
"""name -> component class, as accepted by ``corlet --std``"""

__all__ = [
    "ADMIN_ROUTES",
    "HEALTH_CHECKS",
    "JOBS",
    "STD_COMPONENTS",
    "AdminComponent",
    "AdminConfig",
    "AdminServer",
    "CheckResult",
    "ComponentFilter",
    "ConfigService",
    "ConfigServiceComponent",
    "ConfigWatcher",
    "HealthCheck",
    "HealthComponent",
    "HealthReport",
    "HealthService",
    "Job",
    "JobHandle",
    "JsonFormatter",
    "LayeredConfig",
    "LoggingComponent",
    "ReloadComponent",
    "Request",
    "Response",
    "Route",
    "Scheduler",
    "SchedulerComponent",
    "Status",
    "configure_logging",
]
