"""Ordinary thread-pool code; instrumentation is selected externally."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading


def child(value, gate=None):
    if gate is not None:
        if not gate.wait(5):
            raise TimeoutError("ordinary workload gate timed out")
    if isinstance(value, Exception):
        raise value
    return value + 1


def root(pool, value, gate=None):
    return pool.submit(child, value, gate)


async def async_root():
    return await asyncio.to_thread(child, 40)


def main():
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = root(pool, 10), root(pool, 20)
        gate = threading.Event()
        late = root(pool, 30, gate)
        gate.set()
        values = [future.result(5) for future in (first, second, late)]
        error = ValueError("original worker exception")
        failed = root(pool, error)
        try:
            failed.result(5)
        except ValueError as caught:
            same_error = caught is error
        else:
            raise AssertionError("worker exception was lost")
    values.append(asyncio.run(async_root()))
    assert values == [11, 21, 31, 41] and same_error
    print("results=" + ",".join(map(str, values)) + "; original-error=" + str(same_error))


if __name__ == "__main__":
    main()
