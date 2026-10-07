"""Ordinary Python: configuration selects functions without source instrumentation."""
import asyncio


def recursive(depth):
    return 0 if depth == 0 else 1 + recursive(depth - 1)


async def child():
    await asyncio.sleep(0)
    return 42


async def parent():
    return await child()


async def cancelled(gate):
    await gate.wait()


def generator():
    yield 7


def escaping(error):
    raise error


async def main():
    assert recursive(3) == 3
    assert await parent() == 42
    pending = asyncio.create_task(cancelled(asyncio.Event()))
    await asyncio.sleep(0)
    pending.cancel()
    try:
        await pending
    except asyncio.CancelledError:
        pass
    values = generator()
    assert next(values) == 7
    values.close()
    error = ValueError("original payload")
    try:
        escaping(error)
    except ValueError as caught:
        assert caught is error
    print("trace results preserved")


if __name__ == "__main__":
    asyncio.run(main())
