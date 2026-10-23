/* A snapshot FILE, consumed by unmodified CPython's native run-file path. */
#define _GNU_SOURCE
#define _DARWIN_UNLIMITED_STREAMS 1
#include <Python.h>
#include "InputJournal.h"

#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef __APPLE__
#include <dlfcn.h>
static pthread_once_t resolve_once = PTHREAD_ONCE_INIT;
static FILE *(*original_fopen)(const char *, const char *);

static void resolve_fopen(void) {
    void *address = dlsym(RTLD_NEXT, "fopen");
    _Static_assert(sizeof(address) == sizeof(original_fopen), "unsupported POSIX function pointer ABI");
    memcpy(&original_fopen, &address, sizeof(address));
}

static FILE *native_fopen(const char *path, const char *mode) {
    pthread_once(&resolve_once, resolve_fopen);
    if (original_fopen) return original_fopen(path, mode);
    errno = ENOSYS;
    return NULL;
}
#else
#define native_fopen fopen
#endif

static char *target;
static pthread_t main_thread;
static atomic_int armed;
static int expected_entry;

static FILE *snapshot(FILE *input, const char *path) {
    struct stat metadata;
    unsigned char *bytes = NULL;
    FILE *copy = NULL;
    if (fstat(fileno(input), &metadata) || !S_ISREG(metadata.st_mode) ||
            metadata.st_size < 0 || (uint64_t)metadata.st_size > OTELC_PY_SOURCE_LIMIT) goto failed;
    size_t length = (size_t)metadata.st_size;
    bytes = malloc(length ? length : 1);
    if (!bytes || fread(bytes, 1, length, input) != length ||
            fgetc(input) != EOF || ferror(input)) goto failed;
    copy = tmpfile();
    if (!copy || fwrite(bytes, 1, length, copy) != length ||
            fflush(copy) || fseek(copy, 0, SEEK_SET)) goto failed;
    /* Record only after the exact FILE to be consumed is ready. If evidence
       fails, native execution still consumes that snapshot; the journal fails. */
    (void)otelc_py_journal_source(path, bytes, length, &metadata);
    free(bytes);
    fclose(input);
    return copy;

failed:
    otelc_py_journal_capture_failed();
    if (copy) fclose(copy);
    free(bytes);
    rewind(input);
    return input;
}

static FILE *capture_fopen(const char *path, const char *mode) {
    FILE *input = native_fopen(path, mode);
    int native_errno = errno;
    if (!input || !atomic_load(&armed) || !pthread_equal(pthread_self(), main_thread) ||
            strcmp(path, target) != 0) return input;
    /* CPython releases the GIL around fopen. An application audit hook may
       open the same file first; its Python frame must not consume our arm. */
    PyGILState_STATE gil = PyGILState_Ensure();
    int native_entry = PyEval_GetFrame() == NULL;
    PyGILState_Release(gil);
    int ready = 1;
    if (native_entry && atomic_compare_exchange_strong(&armed, &ready, 0)) input = snapshot(input, path);
    errno = native_errno;
    return input;
}

#ifdef __APPLE__
__attribute__((used)) static struct {
    const void *replacement;
    const void *original;
} interposition __attribute__((section("__DATA,__interpose"))) = {
    (const void *)capture_fopen, (const void *)fopen
};
#else
FILE *fopen(const char *path, const char *mode) {
    return capture_fopen(path, mode);
}
#endif

int otelc_py_capture_enable(const char *path) {
    if (target || !path || !*path) return -1;
    target = strdup(path);
    if (!target) return -1;
    main_thread = pthread_self();
    expected_entry = 1;
    atomic_store(&armed, 1);
    return 0;
}

void otelc_py_capture_finish(void) {
    atomic_store(&armed, 0);
    otelc_py_journal_finish(1, expected_entry);
    free(target);
    target = NULL;
}
