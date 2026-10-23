#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE 1
#include "InputJournal.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>

#define HEADER_SIZE 36U
#define METADATA_SIZE 56U
#define RECORD_LIMIT 4096U

static pthread_mutex_t journal_lock = PTHREAD_MUTEX_INITIALIZER;
static int journal_fd = -1;
static uint64_t sequence, sources, source_bytes, capture_failures, journal_failures;
static size_t journal_bytes;
static dev_t journal_device;
static ino_t journal_inode;

static void integer(unsigned char *output, uint64_t value) {
    for (size_t index = 0; index < 8; ++index) {
        output[7 - index] = (unsigned char)(value & 255U);
        value >>= 8;
    }
}

static int complete_write(const unsigned char *bytes, size_t length) {
    while (length) {
        ssize_t written = write(journal_fd, bytes, length);
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) return -1;
        bytes += written;
        length -= (size_t)written;
    }
    return 0;
}

static int record(unsigned char kind, const char *path, size_t path_size,
                  const unsigned char metadata[METADATA_SIZE],
                  const unsigned char *bytes, size_t size) {
    unsigned char header[HEADER_SIZE] = {'O', 'T', 'L', 'C', 'P', 'Y', '1', 0};
    size_t total = HEADER_SIZE + path_size + METADATA_SIZE + size;
    /* Reserve space for final counters even when source admission is full. */
    size_t reserve = kind == 1 ? HEADER_SIZE + METADATA_SIZE : 0;
    struct stat current;
    if (journal_fd >= 0 && (fstat(journal_fd, &current) ||
            current.st_dev != journal_device || current.st_ino != journal_inode)) {
        /* An application can close and reuse an inherited FD. Do not write
           to, or close, the new application-owned object at that number. */
        ++journal_failures;
        journal_fd = -1;
    }
    if (journal_fd < 0 || sequence >= RECORD_LIMIT ||
            (kind == 1 && sequence + 1 >= RECORD_LIMIT) ||
            total + reserve > OTELC_PY_JOURNAL_LIMIT - journal_bytes) return -1;
    header[8] = kind;
    integer(header + 12, sequence + 1);
    integer(header + 20, path_size);
    integer(header + 28, METADATA_SIZE + size);
    if (complete_write(header, sizeof(header)) ||
            complete_write((const unsigned char *)path, path_size) ||
            complete_write(metadata, METADATA_SIZE) || complete_write(bytes, size)) {
        ++journal_failures;
        /* A partial record cannot be followed by apparently valid evidence. */
        close(journal_fd);
        journal_fd = -1;
        return -1;
    }
    ++sequence;
    journal_bytes += total;
    return 0;
}

int otelc_py_journal_open(int descriptor) {
    struct stat metadata;
    int flags = fcntl(descriptor, F_GETFL);
    if (flags < 0 || (flags & O_ACCMODE) == O_RDONLY || (flags & O_APPEND) ||
            fstat(descriptor, &metadata) || !S_ISREG(metadata.st_mode) ||
            metadata.st_uid != getuid() || (metadata.st_mode & 0777) != 0600 ||
            metadata.st_size != 0 || lseek(descriptor, 0, SEEK_CUR) != 0) return -1;
    pthread_mutex_lock(&journal_lock);
    if (journal_fd >= 0 || fcntl(descriptor, F_SETFD, FD_CLOEXEC)) {
        pthread_mutex_unlock(&journal_lock);
        return -1;
    }
    journal_fd = descriptor;
    journal_device = metadata.st_dev;
    journal_inode = metadata.st_ino;
    sequence = sources = source_bytes = capture_failures = journal_failures = 0;
    journal_bytes = 0;
    pthread_mutex_unlock(&journal_lock);
    return 0;
}

int otelc_py_journal_source(const char *path, const unsigned char *bytes,
                           size_t length, const struct stat *info) {
    size_t path_size = path ? strnlen(path, OTELC_PY_PATH_LIMIT + 1U) : 0;
    unsigned char metadata[METADATA_SIZE];
    if (!path_size || path_size > OTELC_PY_PATH_LIMIT || !info ||
            length > OTELC_PY_SOURCE_LIMIT || (!bytes && length) ||
            !S_ISREG(info->st_mode) || info->st_size < 0 ||
            (uint64_t)info->st_size != length) {
        otelc_py_journal_capture_failed();
        return -1;
    }
    integer(metadata, (uint64_t)getpid());
    integer(metadata + 8, (uint64_t)info->st_dev);
    integer(metadata + 16, (uint64_t)info->st_ino);
    integer(metadata + 24, length);
#ifdef __APPLE__
    integer(metadata + 32, (uint64_t)info->st_mtimespec.tv_sec);
    integer(metadata + 40, (uint64_t)info->st_mtimespec.tv_nsec);
#else
    integer(metadata + 32, (uint64_t)info->st_mtim.tv_sec);
    integer(metadata + 40, (uint64_t)info->st_mtim.tv_nsec);
#endif
    integer(metadata + 48, (uint64_t)info->st_mode);
    /* No Python allocation, callback or SDK/runtime lock occurs while held. */
    pthread_mutex_lock(&journal_lock);
    int result = record(1, path, path_size, metadata, bytes, length);
    if (result) ++capture_failures;
    else {
        ++sources;
        source_bytes += length;
    }
    pthread_mutex_unlock(&journal_lock);
    return result;
}

void otelc_py_journal_capture_failed(void) {
    pthread_mutex_lock(&journal_lock);
    ++capture_failures;
    pthread_mutex_unlock(&journal_lock);
}

void otelc_py_journal_finish(int python_finalized, int entry_expected) {
    unsigned char metadata[METADATA_SIZE];
    pthread_mutex_lock(&journal_lock);
    if (journal_fd >= 0) {
        integer(metadata, (uint64_t)getpid());
        integer(metadata + 8, sources);
        integer(metadata + 16, source_bytes);
        integer(metadata + 24, capture_failures);
        integer(metadata + 32, journal_failures);
        integer(metadata + 40, !!python_finalized);
        integer(metadata + 48, !!entry_expected);
        (void)record(2, NULL, 0, metadata, NULL, 0);
        if (journal_fd >= 0) close(journal_fd);
        journal_fd = -1;
    }
    pthread_mutex_unlock(&journal_lock);
}
