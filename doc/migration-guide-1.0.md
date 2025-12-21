# Migration Guide: ocabox-tcs 1.0

## Overview

Version 1.0 introduces a **complete refactoring of the service registration and discovery system**. The old path-parsing-based system has been replaced with an explicit, clean architecture.

This is a **breaking change** that requires updates to:
1. Service decorator syntax
2. Configuration file format
3. Service identification

**Benefits:**
- ✅ Explicit service types (no more filename guessing)
- ✅ Clear terminology (service_type, variant, service_id)
- ✅ External services are first-class citizens
- ✅ Dot-separated service IDs for namespacing
- ✅ Centralized registry for external service modules

---

## Quick Migration Checklist

For each service in your project:

- [ ] Update `@config` decorator: `@config` → `@config('service_type')`
- [ ] Update `@service` decorator: `@service` → `@service('service_type')`
- [ ] Update YAML: Add `registry:` section
- [ ] Update YAML: `instance_context` → `variant`
- [ ] Update YAML: Remove `module:` field (move to registry)

---

## Breaking Changes

### 1. Service Decorators Now Require Explicit Type

**Before (0.x):**
```python
from ocabox_tcs.base_service import service, config, BaseServiceConfig

@config
@dataclass
class MyServiceConfig(BaseServiceConfig):
    pass

@service
class MyService(BaseBlockingPermanentService):
    pass
```

**After (1.0):**
```python
from ocabox_tcs.base_service import service, config, BaseServiceConfig

@config('my_service')  # ← Type parameter is REQUIRED
@dataclass
class MyServiceConfig(BaseServiceConfig):
    pass

@service('my_service')  # ← Type parameter is REQUIRED
class MyService(BaseBlockingPermanentService):
    pass
```

**Why:**
- No more brittle filename parsing
- Service type is explicit and clear
- Supports namespacing with dots (e.g., `'examples.minimal'`, `'halina.server'`)

---

### 2. Configuration File Format Changes

**Before (0.x):**
```yaml
nats:
  host: localhost
  port: 4222

services:
  # Internal service
  - type: hello_world
    instance_context: dev
    interval: 5

  # External service
  - type: halina_server
    module: halina.server.halina_server  # ← Field removed in 1.0
    instance_context: prod
    port: 9000
```

**After (1.0):**
```yaml
nats:
  host: localhost
  port: 4222

# NEW: Registry section (maps service_type → Python module path)
registry:
  # Internal services can use ~ (shorthand for ocabox_tcs.services.{type})
  hello_world: ~                              # → ocabox_tcs.services.hello_world

  # External services need explicit module paths
  halina.server: halina.server.halina_server  # → halina.server.halina_server

services:
  # Internal service
  - type: hello_world
    variant: dev      # ← Renamed from instance_context
    interval: 5

  # External service
  - type: halina.server
    variant: prod     # ← Renamed from instance_context
    port: 9000
```

**Key Changes:**
1. **New `registry:` section** - Maps service_type to Python module path
2. **`instance_context` → `variant`** - Clearer terminology
3. **`module:` field removed** - Goes in registry section instead
4. **`~` shorthand** - For internal ocabox_tcs services

---

### 3. Service Identification Changes

**Before (0.x):**
- Service ID format: `module.path.service_name:instance`
- Example: `ocabox_tcs.services.hello_world:dev`
- Parsing was path-based and brittle

**After (1.0):**
- Service ID format: `service_type.variant`
- Example: `hello_world.dev`
- service_type can have dots: `examples.minimal.tutorial`
- variant CANNOT have dots (enforced by validation)

**Why:**
- Simpler, cleaner IDs
- Supports namespacing (e.g., `examples.minimal`, `halina.server`)
- Variant is always the last segment after final dot

---

## Terminology Reference

| Term | Definition | Example |
|------|------------|---------|
| **service_type** | Class identity, can have dots for namespacing | `hello_world`, `examples.minimal`, `halina.server` |
| **variant** | Instance identifier (NO dots allowed) | `dev`, `prod`, `jk15`, `tutorial` |
| **service_id** | Full identifier: `{service_type}.{variant}` | `hello_world.dev`, `halina.server.prod` |
| **module_path** | Python import path (internal, goes in registry) | `ocabox_tcs.services.hello_world`, `halina.server.halina_server` |

---

## Migration Examples

### Example 1: Internal TCS Service (hello_world)

**File:** `src/ocabox_tcs/services/hello_world.py`

**Before:**
```python
@config
@dataclass
class HelloWorldConfig(BaseServiceConfig):
    interval: float = 5.0

@service
class HelloWorldService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            self.svc_logger.info(f"Hello from {self.svc_config.id}")
            await asyncio.sleep(self.svc_config.interval)
```

**After:**
```python
@config('hello_world')  # ← Add explicit type
@dataclass
class HelloWorldConfig(BaseServiceConfig):
    interval: float = 5.0

@service('hello_world')  # ← Add explicit type
class HelloWorldService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            self.svc_logger.info(f"Hello from {self.svc_config.id}")
            await asyncio.sleep(self.svc_config.interval)
```

**Config file:**
```yaml
# No registry entry needed (uses default: ocabox_tcs.services.hello_world)
# Or explicitly:
registry:
  hello_world: ~  # Shorthand for ocabox_tcs.services.hello_world

services:
  - type: hello_world
    variant: dev  # ← Was instance_context
    interval: 5
```

---

### Example 2: External Service (halina_server)

**File:** `halina/server/halina_server.py` (external project)

**Before:**
```python
@config
@dataclass
class HalinaServerConfig(BaseServiceConfig):
    host: str = "0.0.0.0"
    port: int = 9000

@service
class HalinaServer(BaseBlockingPermanentService):
    async def run_service(self):
        # Server logic...
        pass
```

**After:**
```python
@config('halina_server')  # ← Add explicit type
@dataclass
class HalinaServerConfig(BaseServiceConfig):
    host: str = "0.0.0.0"
    port: int = 9000

@service('halina_server')  # ← Add explicit type
class HalinaServer(BaseBlockingPermanentService):
    async def run_service(self):
        # Server logic...
        pass
```

**Config file:**
```yaml
registry:
  halina_server: halina.server.halina_server  # ← Explicit mapping

services:
  - type: halina_server
    variant: prod  # ← Was instance_context
    # module: halina.server.halina_server  ← REMOVE THIS LINE
    port: 9000
```

---

### Example 3: Namespaced Service (examples.minimal)

**File:** `src/ocabox_tcs/services/examples/01_minimal.py`

**Before:**
```python
@service
class MinimalService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            await asyncio.sleep(1)
```

**After:**
```python
@service('examples.minimal')  # ← Type with namespace
class MinimalService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            await asyncio.sleep(1)
```

**Config file:**
```yaml
registry:
  # Filename differs from type, so explicit mapping needed
  examples.minimal: ocabox_tcs.services.examples.01_minimal

services:
  - type: examples.minimal
    variant: tutorial
```

---

## Fallback Behavior & Backward Compatibility

### Registry Fallback
If a service_type is **not** in the registry, the system assumes:
```
module_path = f"ocabox_tcs.services.{service_type}"
```

This means internal TCS services don't need registry entries if they follow the standard path.

**Example:**
```yaml
# These two are equivalent:
registry:
  hello_world: ~

# vs. no registry entry (uses fallback)
services:
  - type: hello_world
    variant: dev
```

### Configuration Field Names
The configuration loader supports **both** field names during transition:
- `variant` (new, preferred)
- `instance_context` (old, deprecated)

**However:** All service code must use `@service('type')` and `@config('type')` - no fallback for decorators.

---

## halina9000 Project Migration

Here's the specific migration for the halina9000 project.

### Files to Update

#### 1. `halina/services/rag_service.py`

**Changes:**
```diff
- @config
+ @config('rag_service')
  @dataclass
  class RagServiceConfig(BaseServiceConfig):
      ...

- @service
+ @service('rag_service')
  class RagService(BaseBlockingPermanentService):
      ...
```

#### 2. `halina/services/config_service.py`

**Changes:**
```diff
- @config
+ @config('config_service')
  @dataclass
  class ConfigServiceConfig(BaseServiceConfig):
      ...

- @service
+ @service('config_service')
  class ConfigService(BaseBlockingPermanentService):
      ...
```

#### 3. `halina/server/halina_server.py`

**Changes:**
```diff
- @config
+ @config('halina_server')
  @dataclass
  class HalinaServerConfig(BaseServiceConfig):
      ...

- @service
+ @service('halina_server')
  class HalinaServer(BaseBlockingPermanentService):
      ...
```

#### 4. `config/services.yaml`

**Before:**
```yaml
nats:
  host: ${NATS_HOST:-nats.oca.lan}
  port: ${NATS_PORT:-4222}

services:
  - type: rag_service
    module: halina.services.rag_service
    instance_context: prod
    semantic_weight: 0.6
    enable_mcp: false

  - type: config_service
    module: halina.services.config_service
    instance_context: prod
    enable_mcp: false

  - type: halina_server
    module: halina.server.halina_server
    instance_context: prod
    host: 0.0.0.0
    port: 9000
    enable_rest: true
```

**After:**
```yaml
nats:
  host: ${NATS_HOST:-nats.oca.lan}
  port: ${NATS_PORT:-4222}

# NEW: Registry section
registry:
  rag_service: halina.services.rag_service
  config_service: halina.services.config_service
  halina_server: halina.server.halina_server

services:
  - type: rag_service
    variant: prod  # ← Changed from instance_context
    # module: halina.services.rag_service  ← REMOVED
    semantic_weight: 0.6
    enable_mcp: false

  - type: config_service
    variant: prod  # ← Changed from instance_context
    # module: halina.services.config_service  ← REMOVED
    enable_mcp: false

  - type: halina_server
    variant: prod  # ← Changed from instance_context
    # module: halina.server.halina_server  ← REMOVED
    host: 0.0.0.0
    port: 9000
    enable_rest: true
```

---

## Testing Your Migration

### 1. Verify Decorators
Make sure all services have explicit type parameters:
```bash
# Search for old-style decorators (should return 0 results)
grep -r "@config$" halina/
grep -r "@service$" halina/

# Verify new-style decorators exist
grep -r "@config(" halina/
grep -r "@service(" halina/
```

### 2. Verify Configuration
```bash
# Check that variant is used (not instance_context)
grep "variant:" config/services.yaml

# Check that registry section exists
grep -A 5 "^registry:" config/services.yaml

# Ensure no 'module:' fields in services list
grep "module:" config/services.yaml  # Should only appear in comments
```

### 3. Run Services
```bash
# Test launcher
tcs_asyncio --config config/services.yaml

# Check service IDs with tcsctl
tcsctl

# Expected format: service_type.variant
# e.g., rag_service.prod, halina_server.prod
```

### 4. Verify Service Discovery
All services should appear in tcsctl with correct service_id format:
```
● halina_server.prod [ok] ...
● rag_service.prod [ok] ...
● config_service.prod [ok] ...
```

---

## Common Migration Issues

### Issue 1: Missing Type Parameter

**Error:**
```
TypeError: @service decorator requires a string service_type argument.
Got: <class 'MyService'>. Did you forget to provide the service type?
```

**Fix:**
```python
# Wrong:
@service
class MyService(BaseBlockingPermanentService):
    pass

# Right:
@service('my_service')
class MyService(BaseBlockingPermanentService):
    pass
```

---

### Issue 2: ServiceClassNotFoundError

**Error:**
```
ServiceClassNotFoundError: No service class found for type 'my_service'.
Module 'my.module.path' was imported but no class was registered.
```

**Fix:**
Ensure the @service decorator matches the type in config:
```python
# config.yaml:
# registry:
#   my_service: my.module.path

@service('my_service')  # ← Must match type in config
class MyService(...):
    pass
```

---

### Issue 3: Variant Contains Dots

**Error:**
```
ValueError: Variant 'my.variant.name' contains dots which is not allowed.
Variants must be simple identifiers without dots (only service_type can have dots).
```

**Fix:**
```yaml
# Wrong:
services:
  - type: hello_world
    variant: my.variant.name  # ← NO DOTS ALLOWED

# Right:
services:
  - type: hello_world.my
    variant: name  # ← Dots only in type
```

---

## Environment Variables

Environment variable pattern remains the same, but uses `variant`:

**Format:** `{TYPE}_{VARIANT}_{FIELD}` or `{TYPE}_{FIELD}`

**Examples:**
```bash
# Service-specific (with variant)
export HELLO_WORLD_DEV_INTERVAL=10

# Service-type-wide (without variant)
export HELLO_WORLD_INTERVAL=5

# Shared across services (no prefix)
export ANTHROPIC_API_KEY=sk-...
```

---

## CLI Usage

Service execution remains similar, but with new field names:

**Before (0.x):**
```bash
python -m halina.server.halina_server config.yaml prod
```

**After (1.0):**
```bash
python -m halina.server.halina_server config.yaml prod
#                                      └─ config  └─ variant
```

Arguments are positional, so the CLI remains backward compatible.

**Named arguments:**
```bash
python -m halina.server.halina_server config.yaml prod --runner-id my-runner
```

---

## Gradual Migration Strategy

If you have many services, migrate incrementally:

### Phase 1: Update Decorators
1. Add explicit types to all `@config` and `@service` decorators
2. Test services individually

### Phase 2: Update Configuration
1. Add `registry:` section to config file
2. Keep both `instance_context` and `variant` temporarily:
   ```yaml
   services:
     - type: my_service
       instance_context: prod  # deprecated
       variant: prod           # new
   ```
3. Test launchers

### Phase 3: Cleanup
1. Remove `instance_context` fields
2. Remove `module:` fields
3. Final testing

---

## Need Help?

- **GitHub Issues:** https://github.com/observe-murphy/ocabox-tcs/issues
- **Documentation:** See `CLAUDE.md` and `doc/development-guide.md`
- **Examples:** Check `src/ocabox_tcs/services/examples/` for updated tutorial services

---

## Version History

- **1.0.0** - Complete service registration refactoring (2025-12-17)
  - Explicit @service/@config decorators
  - New registry: section in config
  - Renamed instance_context → variant
  - Dot-separated service IDs
