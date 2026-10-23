#define _POSIX_C_SOURCE 200809L
#include "InputJournal.h"

#include <fcntl.h>
#include <signal.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

#define CHECK(condition) do { if (!(condition)) return __LINE__; } while (0)

int main(int argc, char **argv) {
    CHECK(argc == 3);
    int fd = open(argv[1], O_WRONLY | O_CREAT | O_EXCL, 0600);
    CHECK(fd >= 0);
    CHECK(otelc_py_journal_open(-1) == -1);
    int pipe_fds[2];
    CHECK(pipe(pipe_fds) == 0);
    CHECK(otelc_py_journal_open(pipe_fds[1]) == -1);
    close(pipe_fds[0]); close(pipe_fds[1]);
    int readonly = open(argv[1], O_RDONLY);
    CHECK(otelc_py_journal_open(readonly) == -1);
    close(readonly);
    CHECK(fchmod(fd, 0644) == 0);
    CHECK(otelc_py_journal_open(fd) == -1);
    CHECK(fchmod(fd, 0600) == 0);
    CHECK(write(fd, "x", 1) == 1);
    CHECK(otelc_py_journal_open(fd) == -1);
    CHECK(ftruncate(fd, 0) == 0);
    CHECK(otelc_py_journal_open(fd) == -1); /* Empty file, stale offset. */
    CHECK(lseek(fd, 0, SEEK_SET) == 0);
    CHECK(otelc_py_journal_open(fd) == 0);
    int other = dup(fd);
    CHECK(otelc_py_journal_open(other) == -1);
    close(other);
    struct stat metadata;
    CHECK(fstat(fd, &metadata) == 0);
    metadata.st_size = 3;
    const unsigned char bytes[] = {0, 255, 10};
    if (strcmp(argv[2], "invalid") == 0) {
        CHECK(otelc_py_journal_source(NULL, bytes, 3, &metadata) == -1);
        CHECK(otelc_py_journal_source("entry.py", NULL, 3, &metadata) == -1);
        CHECK(otelc_py_journal_source("entry.py", bytes, OTELC_PY_SOURCE_LIMIT + 1U, &metadata) == -1);
        CHECK(otelc_py_journal_source("entry.py", bytes, 3, NULL) == -1);
    } else if (strcmp(argv[2], "capacity") == 0) {
        metadata.st_size = 0;
        for (int index = 0; index < 4095; ++index) {
            CHECK(otelc_py_journal_source("empty.py", NULL, 0, &metadata) == 0);
        }
        CHECK(otelc_py_journal_source("empty.py", NULL, 0, &metadata) == -1);
    } else if (strcmp(argv[2], "write-failure") == 0) {
        close(fd);
        CHECK(otelc_py_journal_source("entry.py", bytes, 3, &metadata) == -1);
    } else if (strcmp(argv[2], "file-limit") == 0) {
        struct rlimit original;
        CHECK(getrlimit(RLIMIT_FSIZE, &original) == 0);
        struct rlimit limit = original;
        limit.rlim_cur = 0;
        void (*handler)(int) = signal(SIGXFSZ, SIG_IGN);
        CHECK(handler != SIG_ERR && setrlimit(RLIMIT_FSIZE, &limit) == 0);
        CHECK(otelc_py_journal_source("entry.py", bytes, 3, &metadata) == -1);
        CHECK(setrlimit(RLIMIT_FSIZE, &original) == 0);
        CHECK(signal(SIGXFSZ, handler) != SIG_ERR);
    } else if (strcmp(argv[2], "byte-capacity") == 0) {
        unsigned char *large = calloc(OTELC_PY_SOURCE_LIMIT, 1);
        CHECK(large != NULL);
        metadata.st_size = OTELC_PY_SOURCE_LIMIT;
        for (int index = 0; index < 3; ++index) {
            CHECK(otelc_py_journal_source("large.py", large, OTELC_PY_SOURCE_LIMIT, &metadata) == 0);
        }
        CHECK(otelc_py_journal_source("large.py", large, OTELC_PY_SOURCE_LIMIT, &metadata) == -1);
        free(large);
    } else if (strcmp(argv[2], "reuse") == 0) {
        close(fd);
        size_t length = strlen(argv[1]);
        char *name = malloc(length + 9);
        CHECK(name != NULL);
        memcpy(name, argv[1], length);
        memcpy(name + length, ".foreign", 9);
        int other_fd = open(name, O_RDWR | O_CREAT | O_EXCL, 0600);
        free(name);
        CHECK(other_fd >= 0);
        if (other_fd != fd) {
            CHECK(dup2(other_fd, fd) == fd);
            close(other_fd);
        }
        CHECK(write(fd, "app", 3) == 3);
        CHECK(otelc_py_journal_source("entry.py", bytes, 3, &metadata) == -1);
        otelc_py_journal_finish(1, 1);
        CHECK(fcntl(fd, F_GETFD) >= 0);
        CHECK(fstat(fd, &metadata) == 0 && metadata.st_size == 3);
        close(fd);
    } else {
        CHECK(otelc_py_journal_source("entry.py", bytes, 3, &metadata) == 0);
    }
    otelc_py_journal_finish(1, 1);
    otelc_py_journal_finish(0, 0); /* Finalization is idempotent. */
    return 0;
}
