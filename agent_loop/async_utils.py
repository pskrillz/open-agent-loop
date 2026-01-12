"""
Async utility functions for agent loop.
Pure functions for common async patterns like interrupt handling.
"""

import asyncio
from contextlib import suppress
from typing import Optional, Any, Tuple, Coroutine
from halo import Halo


async def execute_with_interrupt(
    task_coro: Coroutine,
    interrupt_event: asyncio.Event,
    spinner: Optional[Halo] = None,
) -> Tuple[bool, Any]:
    """
    Execute an async task with interrupt support.
    
    This is a pure function that handles the common pattern of:
    - Creating a task
    - Monitoring for interrupts
    - Cancelling on interrupt
    - Waiting for clean cancellation
    
    Args:
        task_coro: Coroutine to execute
        interrupt_event: Event that signals interruption
        spinner: Optional Halo spinner to stop on interrupt
    
    Returns:
        (was_interrupted, result_or_none) tuple
        - was_interrupted: True if task was interrupted, False if completed
        - result_or_none: Task result if completed, None if interrupted
    
    Example:
        was_interrupted, result = await execute_with_interrupt(
            some_async_function(),
            interrupt_event,
            spinner
        )
        if was_interrupted:
            # Handle interrupt
        else:
            # Use result
    """
    interrupt_event.clear()
    task = asyncio.create_task(task_coro)
    
    # Wait for task completion or interrupt
    while not task.done():
        await asyncio.sleep(0.1)
        if interrupt_event.is_set():
            task.cancel()
            break
    
    # Handle interrupt
    if interrupt_event.is_set():
        if spinner:
            spinner.stop()
        # Wait for task to fully cancel
        with suppress(asyncio.CancelledError):
            await task
        return True, None
    
    # Task completed normally
    return False, task.result()
