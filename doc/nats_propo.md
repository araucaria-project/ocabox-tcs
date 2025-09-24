
# NATS in services framework

## NATS Communication

### Who Talks via NATS?

1. **MonitoredObjects**:
   - Periodic status reports
   - Register on start for discovery  
   - Signal shutdown
   - RPC commands via serverish (health-check, stats)

2. **ServiceController**: 
   - Future: RPC controlling commands for service-specific functionality

3. **ServiceLauncher**:
   - Publishes "declared" services (including non-running ones)
   - Own status via MonitoredObject

### Discovery via JetStream
- Uses NATS JetStream messages as registration for discovery
- No central registry service
- Distributed discovery based on message history

## Goals
* Easy discovery of running and defined but not running services
* Standardized monitoring of services and their sub-components
* Standardized RPC communication for commands and queries
* Loose coupling between services and their launchers
* Monitoring of ad-hoc components (following only the monitored object conventions) as well as declarative services in common way.
* Alive messages to detect silent failures (e.g. deadlocks)
* Eady to implement "services monitor" which has no prior knowledge of what services are running, but can discover them and monitor their status.
* Grouping of monitored objects (e.g. service with sub-monitors) with aggregation of status and alive messages.

## What is published - timeline
Below is the example for four services (service names consist of package name and instance context, but in general, 
monitored objects can have any name, with any number of dots).:
* permanent service: `svc1.main`, with two sub-monitors (sub-monitors are not services, but MonitoredObjects, they do not publish themselves directly)
* single-shot service: `svc2.once`
* cyclic service: `svc3.cyclic`
* disabled service: `svc4.disabled` (configured in launcher but not running)

All managed by the same launcher named `launcher01.server01.oca` (deliberately with dots to show that dots are allowed in names of monitored objects).

```
time 0s: launcher starts
time 1s: launcher publishes its MonitoredObject: svc.registry.launcher01_server01_oca.start
time 2s: launcher publishes its MonitoredObject: svc.status.launcher01_server01_oca
time 3s: launcher publishes: svc.registry.svc1_main.declared
time 4s: launcher publishes: svc.registry.svc2_once.declared
time 5s: launcher publishes: svc.registry.svc3_cyclic.declared
time 6s: launcher publishes: svc.registry.svc4_disabled.declared
time 7s: launcher publishes its status: svc.status.launcher01_server01_oca on status changes
time 7s: launcher starts publishing periodic alive message: svc.alive.launcher01_server01_oca every 30s
time 8s: svc1.main starts
time 8s: svc1.main publishes svc.registry.svc1_main.start
time 9s: svc1.main publishes its MonitoredObject: svc.status.svc1_main
time 10s: svc1.main starts publishing its status: svc.status.svc1_main on status changes
time 11s: svc1.main starts publishing periodic alive message: svc.alive.svc1_main every 30s
time 12s: svc1.main's sub-monitor1 starts
time 13s: svc1.main's sub-monitor1 status and alive messages are aggregated into svc1.main's messages
time 14s: svc1.main's sub-monitor2 starts
time 15s: svc1.main's sub-monitor2 status and alive messages are aggregated into svc1.main's messages
time 16s: svc2.once starts
time 16s: svc2.once publishes svc.registry.svc2_once.start
time 17s: svc2.once publishes its MonitoredObject: svc.status.svc2_once
time 18s: svc2.once publishes its status: svc.status.svc2_once on status changes
time 19s: svc2.once starts publishing periodic alive message: svc.alive.svc2_once every 30s
time 20s: svc2.once finishes its work
time 21s: svc2.once publishes svc.registry.svc2_once.stop
time 22s: svc2.once publishes its MonitoredObject: svc.status.svc2_once with final status (e.g. "completed")
time 23s: svc2.once stops
time 24s: svc3.cyclic starts
time 24s: svc3.cyclic publishes svc.registry.svc3_cyclic.start
time 25s: svc3.cyclic publishes its MonitoredObject: svc.status.svc3_cyclic
time 26s: svc3.cyclic starts publishing its status: svc.status.svc3_cyclic on status changes
time 27s: svc3.cyclic starts publishing periodic alive message: svc.alive.svc3_cyclic every 30s
time 28s: svc4.disabled is not started, no messages published
time 37s: launcher publishes its periodic alive message: svc.alive.launcher01_server01_oca
time 38s: svc1.main publishes its periodic alive message: svc.alive.svc1_main
time 49s: svc2.once would publish its periodic alive message: svc.alive.svc2_once but it is already stopped
time 58s: svc3.cyclic publishes its periodic alive
...
```
given monitored object name `<monitored_object_name>` (e.g. `svc1.main`), the following subjects are published:
- `svc.registry.<monitored_object_name>.declared` - published once by **launcher** when service is declared in config
- `svc.registry.<monitored_object_name>.start` - published once by service controller via `MonitoredObject` when it starts
- `svc.registry.<monitored_object_name>.stop` - published once by service controller via `MonitoredObject` when it stops
- `svc.status.<monitored_object_name>` - published on status changes by service controller via `MonitoredObject` (or launcher by its `MonitoredObject`)
- `svc.alive.<monitored_object_name>` - published periodically (every 30s) by `MonitoredObject` 

Services can support common way to receive RPC commands via serverish (e.g. health-check, stats). 
Launchers as well will implement commands specific for them (e.g. list services, start/stop service, restart service).
Common RPC communication is done via subjects:
- `svc.rpc.<monitored_object_name>.<command>` - command request
The response is sent back to the reply subject provided in the request message. 
For the incoming RPC communication, the `serverish` class `MsgRpcResponder` is used.

Additionally, services can publish they specific messages on their own subjects depending on their functionality.

