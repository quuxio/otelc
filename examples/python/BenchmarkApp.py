"""Ordinary seeded workload. No telemetry imports, decorators or startup code."""

import gc
import json
import sys
import time


class WorkItem:
    def __init__(self, seed):
        self.seed = seed

    def close(self):
        pass


class LifecycleWorkItem:
    def __init__(self, seed):
        if seed < 0:
            raise ValueError('construction failed')
        self.fail_close = seed == 1

    def close(self):
        if self.fail_close:
            self.fail_close = False
            raise RuntimeError('close failed')


def lifecycle():
    construction_failed = close_failed = False
    try:
        LifecycleWorkItem(-1)
    except ValueError:
        construction_failed = True
    item = LifecycleWorkItem(1)
    try:
        item.close()
    except RuntimeError:
        close_failed = True
    item.close()
    item.close()
    LifecycleWorkItem(0)
    print(json.dumps({'construction_failed': construction_failed, 'close_failed': close_failed}), flush=True)


def workload(seed):
    if seed < 0:
        raise ValueError('negative seed')
    item = WorkItem(seed)
    try:
        value = item.seed
        total = 0
        for _ in range(128):
            value = (value * 1664525 + 1013904223) & 65535
            total += value
        return total
    finally:
        item.close()


def excluded(seed):
    return seed


for line in sys.stdin:
    command = line.strip()
    if command == 'quit':
        break
    if command == 'lifecycle':
        lifecycle()
        continue
    if command == 'gc':
        gc.collect()
        print(json.dumps({'gc_requested': True}), flush=True)
        continue
    if command == 'throw':
        try:
            workload(-1)
        except ValueError as error:
            print(json.dumps({'exception': type(error).__name__, 'message': str(error)}), flush=True)
        continue
    count = int(command)
    if not 1 <= count <= 1_000_000:
        raise ValueError('count out of range')
    start = time.perf_counter_ns()
    checksum = sum(workload(index) + excluded(0) for index in range(count))
    elapsed = time.perf_counter_ns() - start
    print(json.dumps({'checksum': checksum, 'elapsed_ns': elapsed, 'calls': count}), flush=True)
