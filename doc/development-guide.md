# Service Development Guide

## Choosing the Right Base Class

### Quick Decision Tree

**Does your service run once and finish?**
→ Use `BaseSingleShotService`

**Does your service run continuously?**
→ **Does it have a simple main loop?**
  → Use `BaseBlockingPermanentService` 
→ **Does it need complex task management?**
  → Use `BasePermanentService`

## Base Class Details

### 🔄 BaseSingleShotService
**Use when**: Service runs once and terminates (imports, migrations, one-time tasks)

**Override**: `async def execute(self)`

**Example** (File: `data_importer.py`):
```python
@service
class DataImporterService(BaseSingleShotService):
    async def execute(self):
        # Import data once
        await self.import_data_from_file()
        self.logger.info("Import completed")
```

---

### 🔁 BaseBlockingPermanentService ⭐ **RECOMMENDED for most services**
**Use when**: Service has a main loop and runs continuously

**Override**: 
- `async def run_service(self)` - main loop
- `async def on_start(self)` - setup (optional)
- `async def on_stop(self)` - cleanup (optional)

**Benefits**: 
- ✅ Automatic task management
- ✅ Clean cancellation handling
- ✅ Error handling
- ✅ No manual `asyncio.create_task()`

**Example** (File: `telescope_monitor.py`):
```python
@service
class TelescopeMonitorService(BaseBlockingPermanentService):
    async def on_start(self):
        # Setup connections, initialize hardware
        await self.connect_to_telescope()

    async def run_service(self):
        while self.is_running:
            # Main monitoring loop
            status = await self.check_telescope_status()
            await self.process_status(status)
            await asyncio.sleep(1)

    async def on_stop(self):
        # Cleanup
        await self.disconnect_from_telescope()
```

---

### ⚙️ BasePermanentService
**Use when**: Service needs complex task management or multiple concurrent tasks

**Override**: 
- `async def start_service()` - custom startup
- `async def stop_service()` - custom cleanup

**When to choose**:
- Multiple concurrent tasks
- Complex task coordination
- Custom task lifecycle needs
- Advanced patterns

**Example** (File: `complex_processor.py`):
```python
@service
class ComplexProcessorService(BasePermanentService):
    async def start_service(self):
        # Start multiple concurrent tasks
        self.task1 = asyncio.create_task(self.process_queue_a())
        self.task2 = asyncio.create_task(self.process_queue_b())
        self.monitor_task = asyncio.create_task(self.monitor_health())

    async def stop_service(self):
        # Custom cleanup for multiple tasks
        for task in [self.task1, self.task2, self.monitor_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
```

## Configuration Guide

### Optional Config Classes
Config classes are **optional**. If you don't need custom configuration:

**Example** (File: `simple_service.py`):
```python
@service  # No @config needed
class SimpleService(BaseBlockingPermanentService):
    async def run_service(self):
        # Uses BaseServiceConfig automatically
        self.logger.info(f"Service type: {self.config.type}")
```

### Custom Config Classes
Add custom configuration when needed:

**Example** (File: `advanced_service.py`):
```python
@config
@dataclass
class AdvancedConfig(BaseServiceConfig):
    timeout: int = 30
    max_retries: int = 3
    api_url: str = "https://api.example.com"

@service
class AdvancedService(BaseBlockingPermanentService):
    async def run_service(self):
        # Access custom config
        response = await self.call_api(self.config.api_url)
```

## Best Practices

### ✅ Do
- Use `BaseBlockingPermanentService` for most services with loops
- Use absolute imports: `from ocabox_tcs.base_service import service`
- Keep `run_service()` focused on the main loop
- Use `on_start()`/`on_stop()` for setup/cleanup
- Check `self.is_running` in loops
- Handle `asyncio.CancelledError` gracefully

### ❌ Don't
- Manually create tasks in `BaseBlockingPermanentService` (framework handles it)
- Use relative imports (`from ..base_service`)
- Block in `on_start()` or `on_stop()` 
- Ignore cancellation exceptions
- Put setup/cleanup logic in `run_service()`

## Migration from Old Patterns

### Old Pattern (manual task management):
```python
class OldService(BasePermanentService):
    async def start_service(self):
        self._task = asyncio.create_task(self._main_loop())
    
    async def stop_service(self):
        if self._task:
            self._task.cancel()
            await self._task
    
    async def _main_loop(self):
        while self.is_running:
            # work
            await asyncio.sleep(1)
```

### New Pattern (framework-managed):
```python
class NewService(BaseBlockingPermanentService):
    async def run_service(self):
        while self.is_running:
            # same work
            await asyncio.sleep(1)
```

**Result**: 50% less code, automatic error handling, cleaner cancellation! 🎉