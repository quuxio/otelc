"""Ordinary asyncio work; instrumentation is selected only by external policy."""
import asyncio


async def leaf(value):
    await asyncio.sleep(0)
    return value + 1


async def group(value):
    async with asyncio.TaskGroup() as tasks:
        one = tasks.create_task(leaf(value))
        two = tasks.create_task(leaf(value + 1))
    return one.result() + two.result()


async def detached(value):
    return asyncio.create_task(leaf(value))


async def main():
    results = await asyncio.gather(group(10), group(20))
    task = await detached(30)
    results.append(await task)
    print("results=" + ",".join(str(value) for value in results))


if __name__ == "__main__":
    asyncio.run(main())
