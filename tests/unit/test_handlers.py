from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.workers.handlers import (
    PermanentTaskError,
    TransientTaskError,
    cpu_bound_hash,
    run_task,
)


@pytest.fixture
def pool():
    # A thread pool is enough for tests; production uses a ProcessPoolExecutor.
    with ThreadPoolExecutor(max_workers=1) as executor:
        yield executor


async def test_sleep_task(pool) -> None:
    output = await run_task("t1", {"kind": "sleep", "duration_seconds": 0}, pool)
    assert output["slept_seconds"] == 0


async def test_hash_task_is_deterministic(pool) -> None:
    output = await run_task("t1", {"kind": "hash", "iterations": 10}, pool)
    assert output["digest"] == cpu_bound_hash(10, "t1")


async def test_invalid_payload_is_permanent(pool) -> None:
    with pytest.raises(PermanentTaskError):
        await run_task("t1", {"kind": "delete-production"}, pool)


async def test_failure_rate_one_is_transient(pool) -> None:
    with pytest.raises(TransientTaskError):
        await run_task("t1", {"kind": "sleep", "duration_seconds": 0, "failure_rate": 1}, pool)
