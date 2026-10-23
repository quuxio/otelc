#ifndef OTELC_PYTHON_INPUT_JOURNAL_H
#define OTELC_PYTHON_INPUT_JOURNAL_H

#include <stddef.h>
#include <stdint.h>
#include <sys/stat.h>

#define OTELC_PY_SOURCE_LIMIT (16U * 1024U * 1024U)
#define OTELC_PY_JOURNAL_LIMIT (64U * 1024U * 1024U)
#define OTELC_PY_PATH_LIMIT 65535U

/* Takes ownership only on success. No Python API is used by this writer. */
int otelc_py_journal_open(int descriptor);
int otelc_py_journal_source(const char *path, const unsigned char *bytes,
                           size_t length, const struct stat *metadata);
int otelc_py_journal_import(const char *path, const unsigned char *bytes,
                           size_t length, const struct stat *metadata);
int otelc_py_journal_code(const char *path, const unsigned char *bytes, size_t length,
                         uint64_t identity, uint64_t offset, uint64_t version,
                         uint64_t descriptor_version);
void otelc_py_journal_capture_failed(void);
void otelc_py_journal_finish(int python_finalized, int entry_expected);

int otelc_py_capture_enable(const char *path);
void otelc_py_capture_finish(void);
int otelc_py_imports_install(void);
int otelc_py_imports_ready(void);
int otelc_py_code_install(void);

#endif
