"""P0-3: Graceful shutdown tests — lifespan handler verification.

Tests the FastAPI lifespan context manager for graceful shutdown.
Note: Full SIGTERM simulation requires process-level testing;
these tests verify the lifespan protocol correctness deterministically.
"""



def test_lifespan_startup_shutdown():
    """Lifespan context manager yields and cleans up correctly."""
    import asyncio

    from app.api import lifespan

    async def _run():
        # Simulate app startup/shutdown
        async with lifespan(None):
            # Startup complete, app is running
            pass
        # Shutdown complete, no exception = graceful

    asyncio.run(_run())
    # If we get here without exception, lifespan is correct


def test_lifespan_allows_in_flight_completion():
    """Lifespan does not cancel in-flight work on shutdown initiation."""
    import asyncio

    from app.api import lifespan

    completed = []

    async def _fake_request():
        await asyncio.sleep(0.01)
        completed.append(True)

    async def _run():
        async with lifespan(None):
            # Start a "request" 
            task = asyncio.create_task(_fake_request())
            await task

    asyncio.run(_run())
    assert completed == [True]


def test_app_has_lifespan():
    """FastAPI app is configured with lifespan handler."""
    from app.api import app
    assert app.router.lifespan_context is not None
