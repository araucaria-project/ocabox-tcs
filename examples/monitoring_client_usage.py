#!/usr/bin/env python3
"""Example usage of ServiceControlClient for external monitoring.

This demonstrates how to use ServiceControlClient in other projects
for programmatic access to TCS service monitoring.
"""

import asyncio
from serverish.messenger import Messenger
from tcsctl import ServiceControlClient, ServiceInfo


async def example_one_shot():
    """Example: One-shot service listing (snapshot)."""
    print("=" * 60)
    print("Example 1: One-shot Service Listing")
    print("=" * 60)

    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        client = ServiceControlClient(messenger, subject_prefix='svc')

        # Get all running services
        services = await client.list_services(include_stopped=False)

        print(f"\nFound {len(services)} running services:")
        for service in services:
            print(f"  - {service.service_id}: {service.status.value}")

        # Get specific service
        if services:
            specific_service = await client.get_service(services[0].service_id)
            if specific_service:
                print(f"\nDetails for {specific_service.service_id}:")
                print(f"  Status: {specific_service.status.value}")
                print(f"  Message: {specific_service.status_message}")
                print(f"  Uptime: {specific_service.uptime_str}")
                print(f"  Heartbeat: {specific_service.heartbeat_status}")


async def example_streaming():
    """Example: Streaming mode (follow services in real-time)."""
    print("\n" + "=" * 60)
    print("Example 2: Streaming Mode (Follow Services)")
    print("=" * 60)

    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        client = ServiceControlClient(messenger, subject_prefix='svc')

        # Define callbacks
        def on_update(service: ServiceInfo):
            print(f"[UPDATE] {service.service_id} -> {service.status.value}")
            if service.status_message:
                print(f"         Message: {service.status_message}")

        def on_start(service: ServiceInfo):
            print(f"[START]  {service.service_id} started")

        def on_stop(service: ServiceInfo):
            print(f"[STOP]   {service.service_id} stopped")

        # Register callbacks
        client.on_service_update = on_update
        client.on_service_start = on_start
        client.on_service_stop = on_stop

        # Start following
        print("\nStarting to follow services...")
        await client.start_following()

        # Access current state anytime
        print("\nInitial state:")
        services = client.get_current_services(include_stopped=False)
        print(f"  {len(services)} running services")

        # Let it run for a while
        print("\nFollowing for 30 seconds... (start/stop services to see updates)")
        await asyncio.sleep(30)

        # Final state
        print("\nFinal state:")
        services = client.get_current_services(include_stopped=False)
        print(f"  {len(services)} running services")

        # Stop following
        print("\nStopping...")
        await client.stop_following()


async def example_custom_monitoring():
    """Example: Custom monitoring application."""
    print("\n" + "=" * 60)
    print("Example 3: Custom Monitoring Application")
    print("=" * 60)

    class CustomMonitor:
        """Custom monitoring application using ServiceControlClient."""

        def __init__(self, messenger: Messenger):
            self.client = ServiceControlClient(messenger, subject_prefix='svc')
            self.error_count = 0
            self.warning_count = 0

            # Register callbacks
            self.client.on_service_update = self.handle_update

        async def start(self):
            """Start monitoring."""
            await self.client.start_following()
            print("Custom monitor started")

        async def stop(self):
            """Stop monitoring."""
            await self.client.stop_following()
            print("Custom monitor stopped")

        def handle_update(self, service: ServiceInfo):
            """Handle service updates with custom logic."""
            # Count errors and warnings
            if service.status.value == 'error':
                self.error_count += 1
                print(f"⚠ ERROR detected in {service.service_id}")
            elif service.status.value == 'warning':
                self.warning_count += 1
                print(f"⚠ WARNING detected in {service.service_id}")

            # Check for zombie processes (running but no heartbeat)
            if service.is_running and service.heartbeat_status == 'dead':
                print(f"⚠ ZOMBIE PROCESS detected: {service.service_id}")

        def get_statistics(self) -> dict:
            """Get monitoring statistics."""
            services = self.client.get_current_services(include_stopped=True)
            running = [s for s in services if s.is_running]
            stopped = [s for s in services if not s.is_running]

            return {
                'total_services': len(services),
                'running': len(running),
                'stopped': len(stopped),
                'errors_detected': self.error_count,
                'warnings_detected': self.warning_count,
            }

    messenger = Messenger()
    async with messenger.context(host='localhost', port=4222):
        monitor = CustomMonitor(messenger)

        await monitor.start()

        # Run for 15 seconds
        print("Monitoring for 15 seconds...")
        await asyncio.sleep(15)

        # Print statistics
        stats = monitor.get_statistics()
        print("\nMonitoring Statistics:")
        for key, value in stats.items():
            print(f"  {key}: {value}")

        await monitor.stop()


async def main():
    """Run all examples."""
    try:
        # Example 1: One-shot listing
        await example_one_shot()

        # Example 2: Streaming mode
        await example_streaming()

        # Example 3: Custom monitoring
        await example_custom_monitoring()

    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("ServiceControlClient Examples")
    print("=" * 60)
    print("\nMake sure NATS server is running and some TCS services are active.")
    print("Try: poetry run tcs_asyncio --config config/services.yaml\n")

    asyncio.run(main())
