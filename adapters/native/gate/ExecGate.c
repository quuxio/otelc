/* SPDX-License-Identifier: AGPL-3.0-only */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static long number(const char *text, long minimum, long maximum) {
  char *end = NULL;
  errno = 0;
  long value = strtol(text, &end, 10);
  return errno || !*text || *end || value < minimum || value > maximum ? -1 : value;
}

static int pipe_direction(int fd, int direction) {
  struct stat info;
  int flags = fcntl(fd, F_GETFL);
  return flags >= 0 && (flags & O_ACCMODE) == direction &&
         fstat(fd, &info) == 0 && S_ISFIFO(info.st_mode);
}

static int64_t milliseconds(void) {
  struct timespec now;
  if (clock_gettime(CLOCK_MONOTONIC, &now)) return -1;
  return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int release_received(int fd, long timeout) {
  int64_t now = milliseconds();
  if (now < 0) return 125;
  int64_t deadline = now + timeout;
  for (;;) {
    now = milliseconds();
    if (now < 0) return 125;
    if (now >= deadline) return 124;
    struct pollfd item = {fd, POLLIN, 0};
    int result = poll(&item, 1, (int)(deadline - now));
    if (result < 0 && errno == EINTR) continue;
    if (result <= 0) return result == 0 ? 124 : 125;
    char command = 0;
    ssize_t count = read(fd, &command, 1);
    if (count < 0 && errno == EINTR) continue;
    if (count != 1 || command != 'G') return 125;
    now = milliseconds();
    return now < 0 ? 125 : now >= deadline ? 124 : 0;
  }
}

static char **environment_from(int fd) {
  struct stat info;
  if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
      (info.st_mode & 0777) != 0600 || info.st_size < 0 || info.st_size > 4 * 1024 * 1024) return NULL;
  size_t size = (size_t)info.st_size, offset = 0, count = 0;
  char *raw = calloc(size + 1, 1);
  if (!raw) return NULL;
  while (offset < size) {
    ssize_t got = read(fd, raw + offset, size - offset);
    if (got < 0 && errno == EINTR) continue;
    if (got <= 0) { free(raw); return NULL; }
    offset += (size_t)got;
  }
  if (size && raw[size - 1]) { free(raw); return NULL; }
  for (offset = 0; offset < size; offset += strlen(raw + offset) + 1) {
    char *equal = strchr(raw + offset, '=');
    if (!equal || equal == raw + offset || ++count > 65536) { free(raw); return NULL; }
  }
  char **values = calloc(count + 1, sizeof(char *));
  if (!values) { free(raw); return NULL; }
  count = 0;
  for (offset = 0; offset < size; offset += strlen(raw + offset) + 1) values[count++] = raw + offset;
  return values;
}

int main(int argc, char **argv) {
  if (argc < 6) return 125;
  long events = number(argv[1], 3, INT_MAX);
  long release = number(argv[2], 3, INT_MAX);
  long timeout = number(argv[3], 1, 300000);
  long environment = number(argv[4], 3, INT_MAX);
  if (events < 0 || release < 0 || environment < 0 || events == release ||
      environment == events || environment == release || timeout < 0 || argv[5][0] != '/' ||
      !pipe_direction((int)events, O_WRONLY) || !pipe_direction((int)release, O_RDONLY)) return 125;
  if (dprintf((int)events, "{\"schema_version\":1,\"kind\":\"otelc.native.exec-ready\","
              "\"pid\":%ld,\"parent_pid\":%ld,\"process_group\":%ld,\"session\":%ld}\n",
              (long)getpid(), (long)getppid(), (long)getpgrp(), (long)getsid(0)) < 0) return 125;
  close((int)events);
  int status = release_received((int)release, timeout);
  close((int)release);
  if (status) return status;
  char **values = environment_from((int)environment);
  close((int)environment);
  if (!values) return 125;
  /* exec preserves the verified PID/group and all application standard streams. */
#ifdef OTELC_GATE_COVERAGE
  extern int __llvm_profile_write_file(void);
  __llvm_profile_write_file();
#endif
  execve(argv[5], argv + 5, values);
  perror("otelc native exec gate");
  return 127;
}
