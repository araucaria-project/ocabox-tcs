import os
import sys

from ocabox_tcs.monitoring.monitored_object import MonitoredObject, DummyMonitoredObject
from ocabox_tcs.monitoring.monitored_object_nats import MessengerMonitoredObject



def create_monitor(
    name: str | None = None,
    *,
    subject_prefix: str = 'svc',
    heartbeat_interval: float = 10.0,
    healthcheck_interval: float = 30.0,
    parent_name: str | None = None,
) -> MonitoredObject:
    """Factory function for creating monitored objects.

    Automatically selects implementation based on environment:
    - If Messenger is available (via ProcessContext): MessengerMonitoredObject
    - Otherwise: DummyMonitoredObject (no-op)

    Args:
        name: Unique! monitor name (used in NATS subjects: {prefix}.status.{name}).
              If None, generates unique name using serverish gen_uid().
        subject_prefix: NATS subject prefix (default: 'svc')
        heartbeat_interval: Heartbeat interval in seconds (default: 10)
        healthcheck_interval: Healthcheck interval in seconds (default: 30)
        parent_name: Optional parent name for hierarchical grouping in displays

    Returns:
        MonitoredObject implementation appropriate for current environment

    Example:
        >>> monitor = create_monitor('my_app')
        >>> async with monitor:
        ...     await do_work()
    """
    # Generate unique name if not provided
    if name is None:
        from serverish.base.idmanger import gen_uid
        exename = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else 'unknown'
        if exename.endswith('.py'):
            exename = exename[:-3]
        name = gen_uid(exename, length=8)

    # Check if Messenger is available via ProcessContext
    try:
        from ocabox_tcs.management.process_context import ProcessContext
        process = ProcessContext()
        if process.messenger and process.messenger.is_open:
            # Messenger available - use NATS monitoring
            return MessengerMonitoredObject(
                name=name,
                messenger=process.messenger,
                check_interval=heartbeat_interval,
                healthcheck_interval=healthcheck_interval,
                subject_prefix=subject_prefix,
                parent_name=parent_name,
            )
    except Exception:
        # ProcessContext not available or Messenger not open
        pass

    # Fallback to dummy (no-op) monitor
    return DummyMonitoredObject(name)
