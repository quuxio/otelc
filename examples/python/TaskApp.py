"""Ordinary thread, timer, executor and asyncio work without telemetry code."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Thread, Timer

pool = ThreadPoolExecutor(max_workers=1)


def child(value):
    if value < 0:
        raise ValueError('negative seed')
    return value * 7


async def async_child(value):
    await asyncio.sleep(0)
    return value * 7


def workload(seed):
    values = [pool.submit(child, seed).result()]
    thread = Thread(target=lambda: values.append(child(seed + 1)))
    timer = Timer(.001, lambda: values.append(child(seed + 2)))
    thread.start()
    timer.start()
    thread.join()
    timer.join()
    values.append(asyncio.run(async_child(seed + 3)))
    return sum(values)


try:
    results = [workload(1), workload(2)]
    try:
        workload(-1)
    except ValueError as error:
        failure = str(error)
    print(json.dumps({'results': results, 'failure': failure}))
finally:
    pool.shutdown()
