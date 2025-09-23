"""Status enums and utilities for monitoring system."""

from enum import Enum
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime


class Status(Enum):
    """Service/component status levels."""
    UNKNOWN = "unknown"
    STARTUP = "startup"
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    SHUTDOWN = "shutdown"
    FAILED = "failed"

    def __str__(self) -> str:
        return self.value
    
    @property
    def is_healthy(self) -> bool:
        """Check if status indicates healthy state."""
        return self in (Status.OK, Status.WARNING)
    
    @property
    def is_operational(self) -> bool:
        """Check if status indicates service is operational."""
        return self in (Status.STARTUP, Status.OK, Status.WARNING)


@dataclass
class StatusReport:
    """Status report for a monitored component."""
    name: str
    status: Status
    message: Optional[str] = None
    timestamp: Optional[datetime] = None
    details: Optional[Dict[str, Any]] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "name": self.name,
            "status": self.status.value,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }
        if self.message:
            result["message"] = self.message
        if self.details:
            result["details"] = self.details
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StatusReport":
        """Create from dictionary."""
        return cls(
            name=data["name"],
            status=Status(data["status"]),
            message=data.get("message"),
            timestamp=datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else None,
            details=data.get("details")
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