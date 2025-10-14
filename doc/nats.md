# NATS Communication in TCS Framework

## Overview

The TCS (Telescope Control Services) framework uses NATS for distributed service monitoring, discovery, and control. All communication follows a standardized pattern using JetStream for persistent messages and Core NATS for RPC.

## Goals

- Easy discovery of running and declared (but not running) services
- Standardized monitoring of services and their sub-components
- Standardized RPC communication for commands and queries
- Loose coupling between services and their launchers
- Support for ad-hoc monitored objects alongside declarative services
- Heartbeat messages to detect silent failures (deadlocks, crashes)
- Hierarchical monitoring with status aggregation
- Zero-configuration service discovery for monitoring tools

## Subject Schema

All subjects follow the pattern: `svc.<category>.<event_or_type>.<service_name>`

Service names always appear at the end to enable wildcard subscriptions.

### Registry Events
Lifecycle events for service discovery (JetStream: `tcs_registry`):

```
svc.registry.declared.<service_name>    # Published by launcher when service is configured
svc.registry.start.<service_name>       # Published when service starts
svc.registry.ready.<service_name>       # Published when service is fully initialized
svc.registry.stopping.<service_name>    # Published when shutdown begins
svc.registry.stop.<service_name>        # Published when service stops
```

### Status Updates
Health and status information (JetStream: `tcs_status`):

```
svc.status.<service_name>               # Published on status changes
```

### Heartbeat Messages
Periodic alive signals (JetStream: `svc_heartbeat`):

```
svc.heartbeat.<service_name>            # Published periodically (default: 30s)
```

### RPC Commands
Request-response communication (Core NATS, no JetStream):

```
svc.rpc.<service_name>.v1.<command>     # RPC command requests
```

### Wildcard Subscription Examples

```
svc.registry.start.>                    # All service starts
svc.registry.>.guider.jk15              # All registry events for guider.jk15
svc.status.>                            # All status updates
svc.heartbeat.guider.>                  # Heartbeats from all guider instances
svc.rpc.guider.jk15.>                   # All RPC calls to guider.jk15
svc.>                                   # All service messages
```

## Service Naming Convention

Services are identified by: `<service_type>.<instance_context>`

Examples:
- `guider.jk15` - Guider service for telescope JK15
- `plan_runner.zb08` - Plan runner for telescope ZB08
- `temp_cleanup.wk06` - Temperature cleanup service for site WK06
- `dome_follower.main` - Main dome follower service
- `focus_controller.jk15` - Focus controller for telescope JK15

Launchers and other monitored objects can use dots in their names:
- `launcher01.server01.oca` - Launcher on server01 at OCA
- `hardware.camera.jk15` - Camera hardware monitor

## Message Formats

All timestamps use tuple format: `[year, month, day, hour, minute, second, microsecond]` (UTC)

### Registry Messages

#### Declared Event
Published by launcher when service is configured:

```json
{
  "event": "declared",
  "service_id": "guider.jk15",
  "service_type": "guider",
  "instance_context": "jk15",
  "launcher_id": "launcher01.server01.oca",
  "timestamp": [2025, 9, 24, 10, 30, 0, 0],
  "declared": {
    "service_class": "GuiderService",
    "base_class": "BaseBlockingPermanentService",
    "module": "ocabox_tcs.services.guider",
    "config": {
      "enabled": true,
      "auto_start": false
    }
  }
}
```

#### Start Event
Published when service starts:

```json
{
  "event": "start",
  "service_id": "guider.jk15",
  "service_type": "guider",
  "instance_context": "jk15",
  "launcher_id": "launcher01.server01.oca",
  "runner_id": "launcher01.process_runner.guider_jk15",
  "timestamp": [2025, 9, 24, 10, 30, 15, 123456],
  "host": "server01.oca.lan",
  "pid": 12345
}
```

#### Ready Event
Published when service is fully initialized:

```json
{
  "event": "ready",
  "service_id": "guider.jk15",
  "timestamp": [2025, 9, 24, 10, 30, 18, 500000],
  "startup_duration_seconds": 3.376
}
```

#### Stopping Event
Published when shutdown begins:

```json
{
  "event": "stopping",
  "service_id": "guider.jk15",
  "timestamp": [2025, 9, 24, 12, 45, 30, 0],
  "reason": "manual_stop"
}
```

#### Stop Event
Published when service stops:

```json
{
  "event": "stop",
  "service_id": "guider.jk15",
  "timestamp": [2025, 9, 24, 12, 45, 32, 250000],
  "uptime_seconds": 8117.126,
  "exit_status": "clean"
}
```

### Status Messages

Published on status changes:

```json
{
  "service_id": "guider.jk15",
  "status": "ok",
  "message": "Guiding on star HD 12345",
  "timestamp": [2025, 9, 24, 10, 35, 22, 0],
  "uptime_seconds": 307.0,
  "aggregated": true,
  "children": [
    {
      "name": "camera",
      "status": "ok",
      "message": "Exposing, T=-15.2°C"
    },
    {
      "name": "mount",
      "status": "ok",
      "message": "Tracking, guiding corrections active"
    }
  ],
  "metrics": {
    "guide_rms_arcsec": 0.45,
    "exposure_time_ms": 1000,
    "guide_rate": 1.2
  }
}
```

**Status Values:**
- `unknown` - Status not yet determined
- `startup` - Service is starting up
- `ok` - Service operating normally
- `warning` - Service operational but with issues
- `error` - Service has errors but still running
- `failed` - Service failed to start or operate
- `shutdown` - Service is shutting down

### Heartbeat Messages

Published periodically (default 30s). Includes lightweight metrics for efficiency:

```json
{
  "service_id": "guider.jk15",
  "timestamp": [2025, 9, 24, 10, 31, 0, 0],
  "uptime_seconds": 345.0,
  "status": "ok",
  "sequence": 11,
  "next_heartbeat_expected": [2025, 9, 24, 10, 31, 30, 0],
  "children_count": 2,
  "metrics": {
    "guide_rms_arcsec": 0.45,
    "exposure_count": 123,
    "lost_star_count": 2,
    "last_correction_arcsec": 0.12
  }
}
```

**Fields:**
- `sequence`: Monotonically increasing counter, resets on service restart
- `next_heartbeat_expected`: When next heartbeat should arrive (for timeout detection)
- `children_count`: Number of sub-monitors (0 if none)
- `metrics`: Optional. Lightweight, frequently-updated metrics (counters, current values, simple stats)

### RPC Commands

Common commands available on all services:

#### Health Check
Request: `svc.rpc.<service_name>.v1.health`

Response:
```json
{
  "service_id": "guider.jk15",
  "status": "ok",
  "timestamp": [2025, 9, 24, 10, 32, 15, 0],
  "checks": {
    "camera": "ok",
    "mount": "ok",
    "network": "ok"
  }
}
```

#### Statistics
Request: `svc.rpc.<service_name>.v1.stats`

For extended metrics that are expensive to compute or too large for heartbeat messages:

Response:
```json
{
  "service_id": "guider.jk15",
  "timestamp": [2025, 9, 24, 10, 32, 20, 0],
  "uptime_seconds": 395.0,
  "stats": {
    "total_exposures": 395,
    "guide_corrections": 1185,
    "lost_star_count": 2,
    "avg_guide_rms": 0.52,
    "exposure_histogram": {
      "bins": [0, 500, 1000, 2000, 5000],
      "counts": [10, 150, 200, 35]
    },
    "guide_error_distribution": {...},
    "per_star_statistics": [...]
  }
}
```

### Launcher-Specific RPC Commands

#### List Services
Request: `svc.rpc.<launcher_id>.v1.list`

Response:
```json
{
  "launcher_id": "launcher01.server01.oca",
  "timestamp": [2025, 9, 24, 10, 33, 0, 0],
  "services": [
    {
      "service_id": "guider.jk15",
      "status": "running",
      "pid": 12345
    },
    {
      "service_id": "plan_runner.zb08",
      "status": "stopped"
    }
  ]
}
```

#### Start Service
Request: `svc.rpc.<launcher_id>.v1.start.<service_id>`

Response:
```json
{
  "launcher_id": "launcher01.server01.oca",
  "service_id": "guider.jk15",
  "result": "started",
  "pid": 12345,
  "timestamp": [2025, 9, 24, 10, 34, 0, 0]
}
```

#### Stop Service
Request: `svc.rpc.<launcher_id>.v1.stop.<service_id>`

Response:
```json
{
  "launcher_id": "launcher01.server01.oca",
  "service_id": "guider.jk15",
  "result": "stopped",
  "timestamp": [2025, 9, 24, 10, 35, 0, 0]
}
```

## Timeline Example

Example timeline for multiple services managed by launcher `launcher01.server01.oca`:

```
t=0s   Launcher starts
t=1s   → svc.registry.start.launcher01.server01.oca
t=2s   → svc.status.launcher01.server01.oca

# Discovery phase - launcher declares all configured services
t=3s   → svc.registry.declared.guider.jk15
t=4s   → svc.registry.declared.plan_runner.zb08
t=5s   → svc.registry.declared.temp_cleanup.wk06
t=6s   → svc.registry.declared.dome_follower.disabled  (configured but disabled)

# Launcher starts periodic heartbeat
t=7s   → svc.heartbeat.launcher01.server01.oca

# Service: guider.jk15 lifecycle
t=10s  → svc.registry.start.guider.jk15
t=11s  → svc.status.guider.jk15 (status: startup)
t=13s  → svc.registry.ready.guider.jk15
t=14s  → svc.status.guider.jk15 (status: ok)
t=15s  → svc.heartbeat.guider.jk15
...
t=45s  → svc.heartbeat.guider.jk15 (sequence: 2)
...

# Service: plan_runner.zb08 lifecycle (single-shot)
t=20s  → svc.registry.start.plan_runner.zb08
t=21s  → svc.status.plan_runner.zb08 (status: startup)
t=22s  → svc.registry.ready.plan_runner.zb08
t=23s  → svc.heartbeat.plan_runner.zb08
t=30s  → svc.status.plan_runner.zb08 (status: ok, "Processing plan...")
...
t=120s → svc.registry.stopping.plan_runner.zb08
t=121s → svc.status.plan_runner.zb08 (status: shutdown)
t=122s → svc.registry.stop.plan_runner.zb08

# Service: temp_cleanup.wk06 (periodic, not started yet)
# No messages - waiting to be triggered

# Service: dome_follower.disabled
# No messages - disabled in configuration
```

## JetStream Configuration

Three separate streams for different retention requirements:

### svc_registry Stream
Persistent service lifecycle events:

```yaml
svc_registry:
    Subjects:
        - svc.registry.>
    Description: "Service lifecycle events (start/stop/declared/ready/stopping)"
    MaxAge: null              # Keep indefinitely for discovery
    MaxBytes: 10485760        # 10 MB
    MaxMsgsPerSubject: 100    # Keep history per service
    NoAck: false
    Discard: old
    DenyDelete: false
    DenyPurge: false
```

### svc_status Stream
Service status updates:

```yaml
svc_status:
    Subjects:
        - svc.status.>
    Description: "Service status updates and health information"
    MaxAge: 2592000           # 30 days
    MaxBytes: 524288000       # 500 MB
    NoAck: false
    Discard: old
    DenyDelete: false
    DenyPurge: false
```

### svc_heartbeat Stream
Heartbeat messages:

```yaml
svc_heartbeat:
    Subjects:
        - svc.heartbeat.>
    Description: "Service heartbeat messages"
    MaxAge: 86400             # 1 day
    MaxBytes: 104857600       # 100 MB
    Storage: file             # File storage (survives NATS restarts)
    NoAck: true
    Discard: old
    DenyDelete: false
    DenyPurge: false
```

### RPC Note

RPC does not use JetStream (request-response pattern):

```yaml
# Note: RPC uses Core NATS, not JetStream
# Subject pattern: svc.rpc.<service_name>.v1.<command>
```

## Implementation Requirements

### MonitoredObject

Must support:
1. `context` dict for metadata injection (service_type, instance_context, launcher_id, runner_id, module, host, pid)
2. `start_time` for uptime calculation
3. `heartbeat_sequence` counter for heartbeat messages
4. Separate publish methods:
   - `publish_registry(event)` - lifecycle events
   - `publish_status()` - status updates
   - `publish_heartbeat()` - heartbeat messages

### ServiceController

Must pass to MonitoredObject:
1. `launcher_id` (from runner_id, or None for manual start)
2. `runner_id` (from launcher)
3. Service metadata (service_type, instance_context, class name, module)
4. `host` and `pid` (from ServicesProcess)

### ServicesProcess

Must provide:
1. `host` - hostname of the server
2. `pid` - process ID
3. Shared NATS messenger instance

### Launcher

Must implement:
1. Publish its own MonitoredObject messages
2. Publish `declared` events for all configured services before starting any
3. Pass `launcher_id` and `runner_id` to service runners
4. Implement RPC handlers (list, start, stop)

## Service Discovery Pattern

A monitoring tool can discover all services without prior knowledge:

1. Subscribe to `svc.registry.>` to see all lifecycle events
2. Query JetStream history for `svc_registry` to find all declared services
3. Subscribe to `svc.status.>` for current status
4. Subscribe to `svc.heartbeat.>` to detect alive services
5. Use `next_heartbeat_expected` to detect timeouts

Example discovery flow:
```python
# Get all declared services from history
declared = await js.stream_messages("svc_registry", "svc.registry.declared.>")

# Get recent status updates
statuses = await js.stream_messages("svc_status", "svc.status.>")

# Subscribe to heartbeat messages
await nc.subscribe("svc.heartbeat.>", cb=handle_heartbeat)

# Monitor for timeouts using next_heartbeat_expected
```

## Hierarchical Monitoring

Services can have sub-monitors that aggregate into parent status:

```python
# Parent service
guider = MessengerMonitoredObject("guider.jk15", messenger=messenger, context=context)

# Child monitors (not published separately)
camera = MonitoredObject("camera", parent=guider)
mount = MonitoredObject("mount", parent=guider)

# Only parent publishes, children statuses are aggregated
await guider.publish_status()  # Includes camera and mount status
```

Published status message automatically includes children and critical health metrics:
```json
{
  "service_id": "guider.jk15",
  "status": "ok",
  "message": "Guiding active",
  "aggregated": true,
  "children": [
    {"name": "camera", "status": "ok", "message": "..."},
    {"name": "mount", "status": "ok", "message": "..."}
  ],
  "metrics": {
    "guide_rms_arcsec": 0.45
  }
}
```

## Metrics Strategy

The framework uses a three-tier approach for metrics:

### 1. Heartbeat Metrics (Lightweight, Regular)
Include in heartbeat messages for efficiency:
- ✅ Cheap to compute (already cached/computed)
- ✅ Frequently changing values
- ✅ Counters (operations completed, errors)
- ✅ Current measurements (temperatures, rates, RMS)
- ✅ Simple statistics (last value, moving average)

**Why:** No extra message overhead, regular updates with heartbeat interval. Even barely-changed metrics are acceptable due to small data size.

### 2. Status Metrics (Critical Only)
Include 1-2 critical metrics in status messages:
- ✅ Key health indicators only
- ✅ Used for quick health assessment
- ✅ Triggers monitoring alerts

**Why:** Status is event-driven (on changes), so only include metrics that indicate health state.

### 3. Extended Metrics (On-Demand via RPC)
Provide detailed statistics via `svc.rpc.<service_name>.v1.stats`:
- ✅ Expensive to compute (histograms, distributions)
- ✅ Large data structures
- ✅ Historical aggregations
- ✅ Detailed breakdowns

**Why:** Only computed when requested, doesn't pollute heartbeat stream.

## Best Practices

1. **Always publish lifecycle events in order:** declared → start → ready → [running] → stopping → stop
2. **Set next_heartbeat_expected** accurately to enable timeout detection
3. **Use aggregated status** - let framework roll up child statuses
4. **Include context in error messages** - helps debugging distributed systems
5. **Version RPC commands** - use `v1`, `v2` for backward compatibility
6. **Lightweight metrics in heartbeats** - counters, simple stats, current values
7. **Critical metrics in status** - only 1-2 key health indicators
8. **Extended metrics via RPC** - expensive computations, large data
9. **Test timeout detection** - verify clients can detect missing heartbeats
10. **Keep subjects clean** - service names at end, no redundant tokens