# Service Restart Policies

This guide explains how to configure automatic service restart behavior for your telescope control services.

## Overview

Services can crash due to bugs, out-of-memory conditions, or other transient failures. Restart policies allow you to automatically restart services when they crash, with configurable delays and limits to prevent restart loops.

## Quick Start

Add restart policy configuration to your `config/services.yaml`:

```yaml
services:
  - type: guider
    instance_context: jk15
    restart: "on-failure"      # Restart on crashes
    restart_sec: 5             # Wait 5 seconds before restarting
    restart_max: 3             # Allow up to 3 restarts
    restart_window: 60         # Within a 60-second window
```

## Restart Policies

Choose one of four restart policies:

### 1. `"no"` - Never Restart (Default)
**Use when:** Testing, development, or critical services that shouldn't auto-restart.

```yaml
services:
  - type: test_camera
    instance_context: dev
    restart: "no"              # Service will not restart if it crashes
```

**Behavior:**
- Service crashes → Remains stopped
- Manual intervention required to restart
- Good for debugging (prevents restart masking issues)

---

### 2. `"on-failure"` - Restart on Non-Zero Exit (Recommended)
**Use when:** Most production services that crash due to errors.

```yaml
services:
  - type: guider
    instance_context: jk15
    restart: "on-failure"
    restart_sec: 10            # Wait 10 seconds between restarts
    restart_max: 5             # Allow up to 5 restarts
    restart_window: 300        # Within a 5-minute window
```

**Behavior:**
- Exit code 0 (success) → NO restart
- Non-zero exit code (failure) → Automatic restart
- Perfect for services that fail unexpectedly but may succeed on retry

**Example triggers:**
- Database connection timeout → Restart (will retry connection)
- File not found → Restart (file might appear later)
- Out of memory → Restart (will retry with clean state)

---

### 3. `"on-abnormal"` - Restart Only on Signals
**Use when:** Services with explicit success/failure exit codes.

```yaml
services:
  - type: worker
    instance_context: main
    restart: "on-abnormal"
    restart_sec: 5
    restart_max: 3
```

**Behavior:**
- Exit code 0 (normal success) → NO restart
- Exit code 1-127 (normal error) → NO restart
- Exit code > 128 (signal/crash) → Automatic restart

**Example triggers:**
- Segmentation fault (SIGSEGV) → Restart
- Out of memory killer (SIGKILL) → Restart
- Normal shutdown (exit 0) → NO restart

---

### 4. `"always"` - Always Restart
**Use when:** Critical services that must stay running (use with caution).

```yaml
services:
  - type: mount_safety
    instance_context: main
    restart: "always"          # Restart even on success
    restart_sec: 2
    restart_max: 0             # Unlimited restarts
```

**Behavior:**
- ANY exit code → Automatic restart
- Service will restart immediately after stopping
- No distinction between success and failure

**⚠️ Warning:** Use only for services designed to run in short cycles. Can cause infinite loops if service crashes immediately.

---

## Configuration Reference

### Required Fields

| Field | Description | Values |
|-------|-------------|--------|
| `restart` | Restart policy | `"no"`, `"on-failure"`, `"on-abnormal"`, `"always"` |

### Optional Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `restart_sec` | float | 5.0 | Seconds to wait before restart |
| `restart_max` | int | 0 | Max restarts in window (0 = unlimited) |
| `restart_window` | float | 60.0 | Time window for counting restarts (seconds) |

## Examples

### Development Server
```yaml
services:
  - type: test_camera
    instance_context: dev
    restart: "no"              # Don't auto-restart during testing
```

### Production Guider
```yaml
services:
  - type: guider
    instance_context: jk15
    restart: "on-failure"      # Restart on crashes
    restart_sec: 5             # Quick restart
    restart_max: 10            # Allow many restarts
    restart_window: 600        # Within 10-minute window
```

### Critical Safety Service
```yaml
services:
  - type: mount_safety
    instance_context: main
    restart: "always"          # Must stay running
    restart_sec: 1             # Restart immediately
    restart_max: 0             # Unlimited
```

### External Package Service
```yaml
services:
  - type: plan_runner
    instance_context: araucaria
    module: araucaria_services.plan_runner  # External package
    restart: "on-failure"
    restart_sec: 10
    restart_max: 5
    restart_window: 300
```

## How It Works

### Restart Detection

**For subprocess services (Process launcher):**
1. Launcher monitors subprocess exit codes every second
2. Detects unexpected process termination
3. Checks restart policy
4. Restarts if policy allows

**For in-process services (Asyncio launcher):**
1. Launcher monitors service running status every second
2. Detects when service stops unexpectedly
3. Checks restart policy
4. Restarts if policy allows

### Restart Limits

Restart limits prevent infinite restart loops:

```yaml
restart_max: 3           # Allow 3 restarts
restart_window: 60       # Count within 60-second window
```

**Example:**
- Service crashes at 10:00:00 → Restart (count: 1)
- Service crashes at 10:00:05 → Restart (count: 2)
- Service crashes at 10:00:10 → Restart (count: 3)
- Service crashes at 10:00:15 → **NO restart** (limit reached)
- Service crashes at 10:01:05 → Restart (first crash outside window)

This prevents cascade failures while still allowing recovery from transient issues.

### Restart Timing

```yaml
restart_sec: 5
```

When a service crashes:
1. **Detect crash** → Log warning, publish NATS event
2. **Wait** → Sleep for `restart_sec` (5 seconds)
3. **Restart** → Spawn new service process
4. **Monitor** → Resume crash detection on new process

The delay allows time for:
- External state to recover (network, database reconnecting)
- Cleanup of file handles and system resources
- Rate limiting to avoid DOS-like restart storms

## NATS Events

When a service crashes or restarts, events are published to NATS:

### CRASH Event
Published when service exits unexpectedly.

```nats
Subject: svc.registry.crashed.ocabox_tcs.services.guider:jk15
Data: {
  "event": "crashed",
  "service_id": "ocabox_tcs.services.guider:jk15",
  "exit_code": 1,
  "restart_policy": "on-failure",
  "will_restart": true
}
```

### RESTARTING Event
Published when service restart is attempted.

```nats
Subject: svc.registry.restarting.ocabox_tcs.services.guider:jk15
Data: {
  "event": "restarting",
  "service_id": "ocabox_tcs.services.guider:jk15",
  "restart_attempt": 2,
  "max_restarts": 5
}
```

### STOP Event
Published when service stops without restart.

```nats
Subject: svc.registry.stop.ocabox_tcs.services.guider:jk15
Data: {
  "event": "stop",
  "service_id": "ocabox_tcs.services.guider:jk15",
  "reason": "exit_code_0"
}
```

You can monitor these events with:
```bash
nats subscribe 'svc.registry.>'
```

## Monitoring Restarts

### Check Service Status
```bash
poetry run tcsctl                    # List all services
poetry run tcsctl --detailed         # Show detailed info
poetry run tcsctl guider             # Filter specific service
```

### Watch NATS Events
```bash
nats subscribe 'svc.registry.crashed.>'    # Watch crashes
nats subscribe 'svc.registry.restarting.>' # Watch restarts
```

### Review Logs
```bash
# Watch service logs in real-time
poetry run tcs_process --config config/services.yaml
```

Look for messages like:
- `exited unexpectedly (exit code: 1)` - Service crashed
- `Published CRASH event` - Crash detected and logged
- `Published RESTARTING event (attempt 1)` - Restart in progress
- `Restart limit reached` - Too many restarts

## Troubleshooting

### Service keeps restarting
**Problem:** Service immediately crashes after restart, hitting restart limit.

**Solutions:**
1. Check logs for the root cause: `poetry run tcs_process --config config/services.yaml`
2. Increase `restart_window` to give service time to stabilize
3. Increase `restart_max` if you're just hitting the limit
4. Use `restart: "no"` to stop auto-restart and debug manually

```yaml
services:
  - type: guider
    instance_context: jk15
    restart: "on-failure"
    restart_sec: 10            # Longer delay to stabilize
    restart_max: 10            # More attempts
    restart_window: 600        # Larger window (10 minutes)
```

### Service never restarts when it crashes
**Problem:** Service crashes but never restarts.

**Check:**
1. Is `restart: "no"`? Change to appropriate policy
2. Is restart limit hit? Check logs for "Restart limit reached"
3. Is the exit code matching policy?
   - `"on-failure"`: Only restarts on non-zero exit
   - `"on-abnormal"`: Only restarts on exit code > 128

### Too much log spam
**Problem:** "Restart limit reached" message repeats many times.

**Solution:**
This is a known limitation. The system still works correctly (limit is enforced), but logs can be noisy. Clear logs and restart launcher.

## Best Practices

1. **Use "on-failure" for most services**
   - Works well for transient failures
   - Doesn't restart on clean shutdown
   - Most predictable behavior

2. **Use "no" for development**
   - Easier to debug crashes
   - Prevents auto-restart masking issues
   - Gives you time to investigate

3. **Use "always" only for critical short-lived services**
   - Can create restart storms if service immediately crashes
   - Only use if service is designed for it

4. **Set reasonable restart limits**
   - Too low: Service won't recover from transient issues
   - Too high: May mask problems with constant restarting
   - Recommend: 3-5 restarts in 60-300 second window

5. **Monitor restart events**
   - Watch NATS events for unexpected restarts
   - Investigate if service is restarting frequently
   - May indicate a deeper problem needing investigation

## See Also

- [Configuration System](development-guide.md) - How services are configured
- [NATS Integration](nats.md) - Publishing and subscribing to events
- [Architecture](architecture.md) - How the launcher system works
