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
static unsigned journal_version;

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
                  const unsigned char *metadata, size_t metadata_size,
                  const unsigned char *bytes, size_t size) {
    unsigned char header[HEADER_SIZE] = {'O', 'T', 'L', 'C', 'P', 'Y', '1', 0};
    header[6] = (unsigned char)('0' + journal_version);
    size_t total = HEADER_SIZE + path_size + metadata_size + size;
    /* Reserve space for final counters even when source admission is full. */
    size_t reserve = kind != 2 ? HEADER_SIZE + METADATA_SIZE : 0;
    struct stat current;
    if (journal_fd >= 0 && (fstat(journal_fd, &current) ||
            current.st_dev != journal_device || current.st_ino != journal_inode)) {
        /* An application can close and reuse an inherited FD. Do not write
           to, or close, the new application-owned object at that number. */
        ++journal_failures;
        journal_fd = -1;
    }
    if (journal_fd < 0 || sequence >= RECORD_LIMIT ||
            (kind != 2 && sequence + 1 >= RECORD_LIMIT) ||
            total + reserve > OTELC_PY_JOURNAL_LIMIT - journal_bytes) return -1;
    header[8] = kind;
    integer(header + 12, sequence + 1);
    integer(header + 20, path_size);
    integer(header + 28, metadata_size + size);
    if (complete_write(header, sizeof(header)) ||
            complete_write((const unsigned char *)path, path_size) ||
            complete_write(metadata, metadata_size) || complete_write(bytes, size)) {
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

int otelc_py_journal_open_version(int descriptor, unsigned version) {
    struct stat metadata;
    if (version != 1 && version != 2) return -1;
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
    journal_version = version;
    sequence = sources = source_bytes = capture_failures = journal_failures = 0;
    journal_bytes = 0;
    pthread_mutex_unlock(&journal_lock);
    return 0;
}

int otelc_py_journal_open(int descriptor) {
    return otelc_py_journal_open_version(descriptor, 1);
}

static int file_input(unsigned char kind, const char *path, const unsigned char *bytes,
                      size_t length, const struct stat *info, uint64_t *event) {
    if (event) *event = 0;
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
    int result = record(kind, path, path_size, metadata, sizeof(metadata), bytes, length);
    if (result) ++capture_failures;
    else {
        ++sources;
        source_bytes += length;
        if (event) *event = sequence;
    }
    pthread_mutex_unlock(&journal_lock);
    return result;
}

int otelc_py_journal_source(const char *path, const unsigned char *bytes,
                           size_t length, const struct stat *info) {
    return file_input(1, path, bytes, length, info, NULL);
}

int otelc_py_journal_import(const char *path, const unsigned char *bytes,
                           size_t length, const struct stat *info) {
    return file_input(3, path, bytes, length, info, NULL);
}

int otelc_py_journal_source_event(const char *path, const unsigned char *bytes,
                                 size_t length, const struct stat *info, uint64_t *event) {
    return file_input(1, path, bytes, length, info, event);
}

int otelc_py_journal_import_event(const char *path, const unsigned char *bytes,
                                 size_t length, const struct stat *info, uint64_t *event) {
    return file_input(3, path, bytes, length, info, event);
}

int otelc_py_journal_origin(unsigned kind, const char *path, const unsigned char *bytes,
                           size_t length, const uint64_t *fields, size_t field_count) {
    size_t path_size = path ? strnlen(path, OTELC_PY_PATH_LIMIT + 1U) : 0;
    size_t expected_fields = kind == 5 ? 9 : 6;
    if (kind < 5 || kind > 7 || !fields || field_count != expected_fields ||
            !path_size || path_size > OTELC_PY_PATH_LIMIT ||
            length > OTELC_PY_SOURCE_LIMIT || (!bytes && length)) {
        otelc_py_journal_capture_failed();
        return -1;
    }
    unsigned char metadata[80];
    integer(metadata, (uint64_t)getpid());
    for (size_t index = 0; index < field_count; ++index) integer(metadata + 8 * (index + 1), fields[index]);
    pthread_mutex_lock(&journal_lock);
    int result = journal_version == 2
        ? record((unsigned char)kind, path, path_size, metadata, 8 * (field_count + 1), bytes, length) : -1;
    if (result) ++capture_failures;
    else {
        ++sources;
        source_bytes += length;
    }
    pthread_mutex_unlock(&journal_lock);
    return result;
}

int otelc_py_journal_code(const char *path, const unsigned char *bytes, size_t length,
                         uint64_t identity, uint64_t offset, uint64_t version,
                         uint64_t descriptor_version) {
    size_t path_size = path ? strnlen(path, OTELC_PY_PATH_LIMIT + 1U) : 0;
    if (!path_size || path_size > OTELC_PY_PATH_LIMIT || !bytes || !length ||
            length > OTELC_PY_SOURCE_LIMIT || !identity) {
        otelc_py_journal_capture_failed();
        return -1;
    }
    unsigned char metadata[METADATA_SIZE];
    integer(metadata, (uint64_t)getpid());
    integer(metadata + 8, identity);
    integer(metadata + 16, offset);
    integer(metadata + 24, version);
    integer(metadata + 32, descriptor_version);
    integer(metadata + 40, length);
    integer(metadata + 48, 0);
    /* Payload preparation is complete before this raw-C critical section. */
    pthread_mutex_lock(&journal_lock);
    int result = record(4, path, path_size, metadata, sizeof(metadata), bytes, length);
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
        (void)record(2, NULL, 0, metadata, sizeof(metadata), NULL, 0);
        if (journal_fd >= 0) close(journal_fd);
        journal_fd = -1;
    }
    pthread_mutex_unlock(&journal_lock);
}
