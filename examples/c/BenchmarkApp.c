#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE 1
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

struct WorkItem { uint32_t seed; };

uint64_t workload(uint32_t seed) {
  struct WorkItem *volatile item = malloc(sizeof *item);
  if (!item) exit(4);
  item->seed = seed;
  uint64_t value = item->seed;
  uint64_t sum = 0;
  for (int step = 0; step < 128; ++step) {
    value = (value * 1664525 + 1013904223) & 65535;
    sum += value;
  }
  free(item);
  return sum;
}

uint64_t excluded(uint32_t seed) { return seed; }

static uint64_t now_ns(void) {
  struct timespec value;
  if (clock_gettime(CLOCK_MONOTONIC_RAW, &value)) return 0;
  return (uint64_t)value.tv_sec * 1000000000 + (uint64_t)value.tv_nsec;
}

int main(void) {
  char line[128];
  while (fgets(line, sizeof(line), stdin)) {
    if (!strcmp(line, "quit\n")) return 0;
    uint32_t count;
    if (sscanf(line, "%" SCNu32, &count) != 1 || count < 1 || count > 1000000) return 2;
    uint64_t checksum = 0;
    uint64_t start = now_ns();
    for (uint32_t index = 0; index < count; ++index) checksum += workload(index) + excluded(0);
    uint64_t end = now_ns();
    if (!start || end <= start) return 3;
    printf("{\"checksum\":%" PRIu64 ",\"elapsed_ns\":%" PRIu64 ",\"calls\":%" PRIu32 "}\n",
           checksum, end - start, count);
    fflush(stdout);
  }
  return 0;
}
