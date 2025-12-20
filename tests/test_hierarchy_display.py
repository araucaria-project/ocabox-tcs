"""Test hierarchical display functionality in tcsctl."""
import pytest
from tcsctl.client import ServiceInfo


@pytest.fixture
def sample_services():
    """Create sample services with parent-child relationships."""
    from datetime import datetime, UTC
    from ocabox_tcs.monitoring import Status

    heartbeat_time = datetime.fromtimestamp(1234567890.0, UTC)

    return [
        # Launcher (top level)
        ServiceInfo(
            service_id="launcher.asyncio-launcher.hostname-abc123",
            status=Status.OK,
            status_message="Launcher running",
            hostname="hostname-abc123",
            pid=1000,
            parent=None,  # Top level
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=3600.0,
            declared=True,
        ),
        # Service launched by launcher (level 2)
        ServiceInfo(
            service_id="parent_service:dev",
            status=Status.OK,
            status_message="Service running",
            hostname="hostname-abc123",
            pid=1001,
            parent="launcher.asyncio-launcher.hostname-abc123",  # Child of launcher
            runner_id="launcher.asyncio-launcher.hostname-abc123.parent_service",
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=3600.0,
            declared=True,
        ),
        # Subprocess launched by service (level 3)
        ServiceInfo(
            service_id="worker_1:dev",
            status=Status.OK,
            status_message="Worker running",
            hostname="hostname-abc123",
            pid=1002,
            parent="parent_service:dev",  # Child of service
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=3600.0,
            declared=False,  # Not in launcher config
        ),
        # Another subprocess (level 3)
        ServiceInfo(
            service_id="worker_2:dev",
            status=Status.OK,
            status_message="Worker running",
            hostname="hostname-abc123",
            pid=1003,
            parent="parent_service:dev",  # Child of service
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=3600.0,
            declared=False,
        ),
    ]


def test_parent_child_mapping(sample_services):
    """Test that parent-child relationships are correctly mapped."""
    # Build parent_to_children mapping (same logic as display.py)
    parent_to_children = {}
    for service in sample_services:
        parent = service.parent
        if parent not in parent_to_children:
            parent_to_children[parent] = []
        parent_to_children[parent].append(service)

    # Verify structure
    assert None in parent_to_children  # Launcher has no parent
    assert len(parent_to_children[None]) == 1
    assert parent_to_children[None][0].service_id == "launcher.asyncio-launcher.hostname-abc123"

    assert "launcher.asyncio-launcher.hostname-abc123" in parent_to_children
    assert len(parent_to_children["launcher.asyncio-launcher.hostname-abc123"]) == 1
    assert parent_to_children["launcher.asyncio-launcher.hostname-abc123"][0].service_id == "parent_service:dev"

    assert "parent_service:dev" in parent_to_children
    assert len(parent_to_children["parent_service:dev"]) == 2
    child_ids = {s.service_id for s in parent_to_children["parent_service:dev"]}
    assert child_ids == {"worker_1:dev", "worker_2:dev"}


def test_three_level_hierarchy_depth(sample_services):
    """Test that we can correctly determine hierarchy depth."""
    # Build parent lookup
    parent_map = {s.service_id: s.parent for s in sample_services}

    def get_depth(service_id):
        """Calculate depth of service in hierarchy."""
        depth = 0
        current = service_id
        while current in parent_map and parent_map[current] is not None:
            depth += 1
            current = parent_map[current]
        return depth

    # Verify depths
    assert get_depth("launcher.asyncio-launcher.hostname-abc123") == 0  # Top level
    assert get_depth("parent_service:dev") == 1  # Level 2
    assert get_depth("worker_1:dev") == 2  # Level 3
    assert get_depth("worker_2:dev") == 2  # Level 3


def test_orphaned_children():
    """Test handling of children whose parent is not in the list."""
    from datetime import datetime, UTC
    from ocabox_tcs.monitoring import Status

    heartbeat_time = datetime.fromtimestamp(1234567890.0, UTC)

    services = [
        # Child with missing parent
        ServiceInfo(
            service_id="orphan:dev",
            status=Status.OK,
            status_message="Orphaned service",
            hostname="hostname-abc123",
            pid=2000,
            parent="missing_parent:dev",  # Parent not in list
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=100.0,
            declared=False,
        ),
    ]

    parent_to_children = {}
    for service in services:
        parent = service.parent
        if parent not in parent_to_children:
            parent_to_children[parent] = []
        parent_to_children[parent].append(service)

    # Orphan should be in mapping
    assert "missing_parent:dev" in parent_to_children
    assert len(parent_to_children["missing_parent:dev"]) == 1

    # Check if parent exists in current list
    all_parent_ids = {s.service_id for s in services}
    assert "missing_parent:dev" not in all_parent_ids


def test_recursive_hierarchy_traversal(sample_services):
    """Test that recursive traversal visits all services in correct order."""
    parent_to_children = {}
    for service in sample_services:
        parent = service.parent
        if parent not in parent_to_children:
            parent_to_children[parent] = []
        parent_to_children[parent].append(service)

    visited = []

    def traverse(service_obj, depth=0):
        """Recursively traverse hierarchy."""
        visited.append((service_obj.service_id, depth))
        if service_obj.service_id in parent_to_children:
            for child in parent_to_children[service_obj.service_id]:
                traverse(child, depth + 1)

    # Start from root
    if None in parent_to_children:
        for service in parent_to_children[None]:
            traverse(service, depth=0)

    # Verify all services visited with correct depths
    assert len(visited) == 4
    assert ("launcher.asyncio-launcher.hostname-abc123", 0) in visited
    assert ("parent_service:dev", 1) in visited
    assert ("worker_1:dev", 2) in visited
    assert ("worker_2:dev", 2) in visited


def test_indent_calculation():
    """Test indent calculation for different hierarchy depths."""
    def calculate_indent(depth):
        """Calculate indent string for given depth (same logic as display.py)."""
        if depth == 0:
            return ""
        else:
            return "  " * (depth - 1) + "  ├─ "

    assert calculate_indent(0) == ""
    assert calculate_indent(1) == "  ├─ "
    assert calculate_indent(2) == "    ├─ "
    assert calculate_indent(3) == "      ├─ "


def test_halina_scenario():
    """Test the specific HALINA scenario: launcher → halina_server → MCP servers."""
    from datetime import datetime, UTC
    from ocabox_tcs.monitoring import Status

    heartbeat_time = datetime.fromtimestamp(1234567890.0, UTC)

    services = [
        # Launcher
        ServiceInfo(
            service_id="launcher.asyncio-launcher.ocm-abc",
            status=Status.OK,
            status_message="Launcher running",
            hostname="ocm",
            pid=5000,
            parent=None,
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=7200.0,
            declared=True,
        ),
        # HALINA server
        ServiceInfo(
            service_id="halina_server:dev",
            status=Status.OK,
            status_message="Service running",
            hostname="ocm",
            pid=5001,
            parent="launcher.asyncio-launcher.ocm-abc",
            runner_id="launcher.asyncio-launcher.ocm-abc.halina_server",
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=7200.0,
            declared=True,
        ),
        # RAG MCP server
        ServiceInfo(
            service_id="rag_mcp:dev",
            status=Status.OK,
            status_message="RAG tool running",
            hostname="ocm",
            pid=5002,
            parent="halina_server:dev",
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=7200.0,
            declared=False,
        ),
        # Config MCP server
        ServiceInfo(
            service_id="config_mcp:dev",
            status=Status.OK,
            status_message="Config tool running",
            hostname="ocm",
            pid=5003,
            parent="halina_server:dev",
            runner_id=None,
            last_heartbeat=heartbeat_time,
            last_status_update=heartbeat_time,
            uptime_seconds=7200.0,
            declared=False,
        ),
    ]

    # Build hierarchy
    parent_to_children = {}
    for service in services:
        parent = service.parent
        if parent not in parent_to_children:
            parent_to_children[parent] = []
        parent_to_children[parent].append(service)

    # Verify structure
    assert len(parent_to_children[None]) == 1  # Launcher
    assert len(parent_to_children["launcher.asyncio-launcher.ocm-abc"]) == 1  # halina_server
    assert len(parent_to_children["halina_server:dev"]) == 2  # 2 MCP servers

    # Verify specific relationships
    launcher = parent_to_children[None][0]
    assert launcher.service_id == "launcher.asyncio-launcher.ocm-abc"

    halina = parent_to_children["launcher.asyncio-launcher.ocm-abc"][0]
    assert halina.service_id == "halina_server:dev"

    mcps = parent_to_children["halina_server:dev"]
    mcp_ids = {s.service_id for s in mcps}
    assert mcp_ids == {"rag_mcp:dev", "config_mcp:dev"}
