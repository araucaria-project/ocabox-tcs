"""Status enums and utilities for monitoring system."""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from serverish.base import dt_from_array, dt_utcnow_array


class Status(Enum):
    """Service/component status levels."""
    UNKNOWN = "unknown"
    STARTUP = "startup"
    OK = "ok"
    DEGRADED = "degraded"
    WARNING = "warning"
    ERROR = "error"
    SHUTDOWN = "shutdown"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value

    @property
    def is_healthy(self) -> bool:
        """Check if status indicates healthy state."""
        return self in (Status.OK, Status.DEGRADED, Status.WARNING)

    @property
    def is_operational(self) -> bool:
        """Check if status indicates service is operational."""
        return self in (Status.STARTUP, Status.OK, Status.DEGRADED, Status.WARNING)


@dataclass
class StatusReport:
    """Status report for a monitored component."""
    name: str
    status: Status
    message: str | None = None
    timestamp: list[int] | None = None  # UTC timestamp in array format [Y, M, D, h, m, s, us]
    details: dict[str, Any] | None = None
    parent: str | None = None  # Optional parent name for hierarchical grouping

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = dt_utcnow_array()
    
    def get_timestamp_dt(self):
        """Get timestamp as datetime object (for display/logging)."""
        if self.timestamp is None:
            return None
        return dt_from_array(self.timestamp)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "name": self.name,
            "status": self.status.value,
            "timestamp": self.timestamp,  # Already in array format
        }
        if self.message:
            result["message"] = self.message
        if self.details:
            result["details"] = self.details
        if self.parent:
            result["parent"] = self.parent
        return result
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StatusReport":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            status=Status(data["status"]),
            message=data.get("message"),
            timestamp=data.get("timestamp"),  # Already in array format
            details=data.get("details"),
            parent=data.get("parent")
        )


def aggregate_status(reports: list[StatusReport]) -> Status:
    """Aggregate multiple status reports into single status."""
    if not reports:
        return Status.UNKNOWN

    statuses = [report.status for report in reports]

    # If any failed, overall is failed
    if Status.FAILED in statuses:
        return Status.FAILED

    # If any error, overall is error
    if Status.ERROR in statuses:
        return Status.ERROR

    # If any warning, overall is warning
    if Status.WARNING in statuses:
        return Status.WARNING

    # If any degraded, overall is degraded
    if Status.DEGRADED in statuses:
        return Status.DEGRADED

    # If any starting up, overall is startup
    if Status.STARTUP in statuses:
        return Status.STARTUP

    # If any shutting down, overall is shutdown
    if Status.SHUTDOWN in statuses:
        return Status.SHUTDOWN

    # If all OK, overall is OK
    if all(s == Status.OK for s in statuses):
        return Status.OK

    # Mixed states, default to warning
    return Status.WARNING