"""Unchanged application: recursion, exceptions, threads, generators and async."""
import asyncio
import concurrent.futures


def process_order(value):
    return value * 2 + 1


def recursive(value):
    return 0 if value == 0 else 1 + recursive(value - 1)


def throwing():
    raise ValueError("application error")


def caught():
    try:
        throwing()
    except ValueError:
        return 7


def excluded():
    return 100


def values():
    yield 2
    yield 3


async def delayed(value):
    await asyncio.sleep(0.001)
    return process_order(value)


async def cancelled():
    await asyncio.sleep(30)


async def asynchronous():
    task = asyncio.create_task(cancelled())
    await asyncio.sleep(0.001)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return sum(await asyncio.gather(delayed(2), delayed(3)))


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        threaded = sum(pool.map(process_order, [1, 2]))
    print(sum(process_order(x) for x in range(4)) + recursive(3) + caught() + excluded() + sum(values()) + asyncio.run(asynchronous()) + threaded)


if __name__ == "__main__":
    main()
