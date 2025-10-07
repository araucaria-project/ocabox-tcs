# Service Initialization Flow

This document describes the initialization flow for all three service execution scenarios.

## Overview

All three scenarios follow the same pattern:
1. **ProcessContext.initialize()** - Once per OS process
2. **ServiceController** creation and initialization - Once per service instance
3. **Service** startup

## Scenario 1: Standalone Service

**Command:** `python src/ocabox_tcs/services/hello_world.py config.yaml instance_context`

```
┌─────────────────────────────────────────────────────────────────┐
│ User runs:                                                       │
│ python src/ocabox_tcs/services/hello_world.py config.yaml test │
└───────────────────────────────┬─────────────────────────────────┘
                                │
                                ▼
                    ┌───────────────────────┐
                    │ BaseService.main()    │
                    │ (class method)        │
                    └───────────┬───────────┘
                                │
                                ▼
            ┌──────────────────────────────────────┐
            │ ProcessContext.initialize()          │
            │ ┌──────────────────────────────────┐ │
            │ │ 1. _init_config_manager()        │ │
            │ │    - Load config.yaml            │ │
            │ │    - Add args config             │ │
            │ │                                  │ │
            │ │ 2. _init_messenger()             │ │
            │ │    - Read NATS host/port         │ │
            │ │    - Connect to NATS             │ │
            │ │                                  │ │
            │ │ 3. _add_nats_config_source()     │ │
            │ │    - Add NATS as config source   │ │
            │ └──────────────────────────────────┘ │
            └──────────────┬───────────────────────┘
                           │ Returns singleton instance
                           ▼
                ┌──────────────────────┐
                │ ServiceController    │
                │ - module_name        │
                │ - instance_id        │
                │ - runner_id          │
                └──────────┬───────────┘
                           │
                           ▼
            ┌──────────────────────────────────┐
            │ controller.initialize()          │
            │ ┌──────────────────────────────┐ │
            │ │ 1. _discover_classes()       │ │
            │ │    - Import service module   │ │
            │ │    - Find @service class     │ │
            │ │    - Find @config class      │ │
            │ │                              │ │
            │ │ 2. _setup_configuration()    │ │
            │ │    - Use ProcessContext's    │ │
            │ │      config_manager          │ │
            │ │    - Resolve service config  │ │
            │ │                              │ │
            │ │ 3. _initialize_monitoring()  │ │
            │ │    - Create MonitoredObject  │ │
            │ │    - Start NATS monitoring   │ │
            │ └──────────────────────────────┘ │
            └──────────────┬───────────────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │ controller.          │
                │   start_service()    │
                │ - Create service     │
                │ - Call start_service │
                └──────────┬───────────┘
                           │
                           ▼
                ┌──────────────────────┐
                │ Service Running      │
                │ (await shutdown)     │
                └──────────────────────┘
```

**Key Points:**
- **Single process** with one service
- **ProcessContext** initialized once
- **ServiceController** initialized once
- Service runs until shutdown signal

---

## Scenario 2: Asyncio Launcher (Multiple Services, Same Process)

**Command:** `poetry run tcs_asyncio`

```
┌────────────────────────────────────┐
│ User runs: poetry run tcs_asyncio  │
└───────────────┬────────────────────┘
                │
                ▼
    ┌───────────────────────┐
    │ AsyncioLauncher       │
    │ .initialize()         │
    └───────────┬───────────┘
                │
                ▼
┌──────────────────────────────────────────────────┐
│ ProcessContext.initialize()  ◄── ONCE PER PROCESS│
│ ┌──────────────────────────────────────────────┐ │
│ │ 1. _init_config_manager()                    │ │
│ │    - Load config.yaml                        │ │
│ │                                              │ │
│ │ 2. _init_messenger()                         │ │
│ │    - Connect to NATS                         │ │
│ │                                              │ │
│ │ 3. _add_nats_config_source()                 │ │
│ └──────────────────────────────────────────────┘ │
│                                                  │
│ ★ SHARED BY ALL SERVICES IN THIS PROCESS ★      │
└──────────────┬───────────────────────────────────┘
               │
               ▼
   ┌───────────────────────────────────────┐
   │ For each service in config:           │
   │                                       │
   │  ┌─────────────────────────────────┐ │
   │  │ AsyncioRunner.start()           │ │
   │  │                                 │ │
   │  │  ServiceController              │ │
   │  │  - module_name                  │ │
   │  │  - instance_id                  │ │
   │  └─────────────┬───────────────────┘ │
   │                │                     │
   │                ▼                     │
   │  ┌─────────────────────────────────┐ │
   │  │ controller.initialize()         │ │
   │  │ - Uses SHARED ProcessContext    │ │
   │  │ - _discover_classes()           │ │
   │  │ - _setup_configuration()        │ │
   │  │ - _initialize_monitoring()      │ │
   │  └─────────────┬───────────────────┘ │
   │                │                     │
   │                ▼                     │
   │  ┌─────────────────────────────────┐ │
   │  │ controller.start_service()      │ │
   │  │ - Service instance created      │ │
   │  │ - Service running (async task)  │ │
   │  └─────────────────────────────────┘ │
   └───────────────────────────────────────┘
                │
                ▼
    ┌───────────────────────────────┐
    │ All Services Running          │
    │                               │
    │ ┌─────┐ ┌─────┐ ┌─────┐      │
    │ │ SVC │ │ SVC │ │ SVC │ ...  │
    │ │  1  │ │  2  │ │  3  │      │
    │ └─────┘ └─────┘ └─────┘      │
    │                               │
    │ All share:                    │
    │ - Same ProcessContext         │
    │ - Same NATS connection        │
    │ - Same config_manager         │
    └───────────────────────────────┘
```

**Key Points:**
- **Single process** with multiple services
- **ProcessContext** initialized once, shared by all
- **Multiple ServiceControllers** (one per service)
- All services share NATS connection and config

---

## Scenario 3: Process Launcher (Multiple Services, Separate Processes)

**Command:** `poetry run tcs_process`

```
┌────────────────────────────────────┐
│ User runs: poetry run tcs_process  │
└───────────────┬────────────────────┘
                │
                ▼
    ┌───────────────────────────────┐
    │ ProcessLauncher.initialize()  │
    └───────────┬───────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────────┐
│ ProcessContext.initialize() ◄── For LAUNCHER process       │
│ ┌────────────────────────────────────────────────────────┐ │
│ │ Launcher needs:                                        │ │
│ │ - Config to know which services to spawn              │ │
│ │ - NATS connection to monitor spawned services         │ │
│ │                                                        │ │
│ │ 1. _init_config_manager()                             │ │
│ │    - Load config.yaml                                 │ │
│ │                                                        │ │
│ │ 2. _init_messenger()                                  │ │
│ │    - Connect to NATS (to monitor spawned services)    │ │
│ │                                                        │ │
│ │ 3. _add_nats_config_source()                          │ │
│ └────────────────────────────────────────────────────────┘ │
│                                                            │
│ ★ LAUNCHER'S ProcessContext (for monitoring services) ★   │
└───────────────────────────┬────────────────────────────────┘
                            │
                            ▼
    ┌───────────────────────────────────────┐
    │ For each service in config:           │
    │                                       │
    │ ┌───────────────────────────────────┐ │
    │ │ ProcessRunner.start()             │ │
    │ │                                   │ │
    │ │ Spawns subprocess:                │ │
    │ │ poetry run python -m              │ │
    │ │   ocabox_tcs.services.hello_world │ │
    │ │   config.yaml instance            │ │
    │ └───────────────┬───────────────────┘ │
    └─────────────────┼───────────────────────┘
                      │
          ┌───────────┴───────────┬───────────────┬─────────────┐
          │                       │               │             │
          ▼                       ▼               ▼             ▼
    ┌─────────┐           ┌─────────┐     ┌─────────┐   ┌─────────┐
    │ PROCESS │           │ PROCESS │     │ PROCESS │   │ PROCESS │
    │    1    │           │    2    │     │    3    │   │   ...   │
    └────┬────┘           └────┬────┘     └────┬────┘   └────┬────┘
         │                     │               │             │
         │                     │               │             │
         │  Each subprocess follows SCENARIO 1 (Standalone) │
         │                     │               │             │
         ▼                     ▼               ▼             ▼
    ┌──────────────────────────────────────────────────────────────┐
    │ Inside each subprocess:                                       │
    │                                                              │
    │   BaseService.main()                                         │
    │           │                                                  │
    │           ▼                                                  │
    │   ProcessContext.initialize()  ◄── Once per subprocess      │
    │           │                                                  │
    │           ▼                                                  │
    │   ServiceController.initialize()                             │
    │           │                                                  │
    │           ▼                                                  │
    │   Service Running                                            │
    │                                                              │
    │   ★ Each subprocess has its OWN:                             │
    │   - ProcessContext (separate instance)                       │
    │   - NATS connection (separate)                               │
    │   - config_manager (separate, but same config file)          │
    └──────────────────────────────────────────────────────────────┘

    ┌──────────────────────────────────────────────────────────────┐
    │ TOTAL ProcessContext instances:                              │
    │                                                              │
    │ 1 (Launcher process) + N (Service processes)                 │
    │ = N+1 ProcessContext instances total                         │
    └──────────────────────────────────────────────────────────────┘
```

**Key Points:**
- **Multiple processes**: 1 launcher process + N service processes
- **Launcher process** has its own ProcessContext (for config + monitoring)
- **Each service process** has its own ProcessContext
- **Total NATS connections**: N+1 (launcher + each service)
- Subprocess initialization follows Scenario 1 (standalone)

---

## Summary Comparison

| Aspect | Standalone | Asyncio Launcher | Process Launcher |
|--------|-----------|------------------|------------------|
| **OS Processes** | 1 | 1 | **N+1** (1 launcher + N services) |
| **Services per process** | 1 | N (all services) | 1 per service process |
| **ProcessContext instances** | 1 | 1 (shared by all services) | **N+1** (1 in launcher + 1 per service) |
| **NATS connections** | 1 | 1 (shared by all services) | **N+1** (1 in launcher + 1 per service) |
| **Config manager instances** | 1 | 1 (shared) | **N+1** (separate, same config file) |
| **Initialization entry** | `BaseService.main()` | `AsyncioLauncher.initialize()` → `ProcessContext.initialize()` | `ProcessLauncher.initialize()` → `ProcessContext.initialize()` + spawn → `BaseService.main()` |
| **Launcher has ProcessContext** | No (no launcher) | Yes (shared with services) | **Yes (separate from services)** |
| **Use case** | Development, testing | Development, low resources | Production, isolation |

---

## Key Design Principles

1. **ProcessContext.initialize()** is the universal entry point for process-wide initialization
2. **ServiceController.initialize()** is service-specific initialization
3. **Same code path** regardless of execution method (standalone/asyncio/process)
4. **ProcessContext is a singleton** - only one per OS process
5. **Clear separation**: Process concerns vs Service concerns

---

## Configuration Bootstrap Flow

All scenarios follow this two-phase bootstrap:

```
Phase 1: File + Args Config
┌────────────────────────────┐
│ config.yaml                │
│ + command line args        │
└──────────┬─────────────────┘
           │
           ▼
    ┌──────────────┐
    │ ConfigManager│
    └──────┬───────┘
           │ resolve_config()
           ▼
    ┌──────────────┐
    │ Get NATS     │
    │ host:port    │
    └──────┬───────┘
           │
           ▼
Phase 2: NATS Config Source
┌────────────────────────────┐
│ Connect to NATS            │
│ (using host:port from file)│
└──────────┬─────────────────┘
           │
           ▼
    ┌──────────────┐
    │ Add NATS     │
    │ config source│
    └──────┬───────┘
           │
           ▼
    ┌──────────────┐
    │ Config Ready │
    │ (file + NATS)│
    └──────────────┘
```

This solves the chicken-and-egg problem:
- Need config file to get NATS connection params
- Once connected, can load additional config from NATS
- All handled transparently in `ProcessContext.initialize()`
