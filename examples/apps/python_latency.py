"""Plain application workload and timing protocol; no instrumentation imports."""
import sys
import time


def process_order(value):
    for _ in range(16):
        value = ((value ^ (value >> 13)) * 2654435761) & 0xFFFFFFFF
    return value


print("ready", flush=True)
for line in sys.stdin:
    if line.strip() == "quit":
        break
    command, count = line.split()
    iterations = int(count)
    if command != "batch" or not 1 <= iterations <= 1000000:
        raise ValueError("invalid workload")
    start = time.perf_counter_ns()
    checksum = 0
    for i in range(iterations):
        checksum ^= process_order(i)
    elapsed = time.perf_counter_ns() - start
    print(f"elapsed_ns={elapsed} checksum={checksum} calls={iterations}", flush=True)
