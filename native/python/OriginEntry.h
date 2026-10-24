#ifndef OTELC_PYTHON_ORIGIN_ENTRY_H
#define OTELC_PYTHON_ORIGIN_ENTRY_H

#include <stddef.h>
#include <stdint.h>
#include <Python.h>

/* The driver owns one interpreter lifetime. Normal journal-1 launches never
   install this explicit, SDK-independent source-entry observer. */
int otelc_py_origin_entry_install(void);
int otelc_py_origin_entry_ready(int optimization);
int otelc_py_origin_entry_active(void);
void otelc_py_origin_entry_cancel(void);
void otelc_py_origin_entry_read(const char *path, const unsigned char *bytes,
                               size_t length, uint64_t sequence);
/* Shared private catalogue and local monitor for checked loader transactions. */
int otelc_py_origin_module(PyObject *code, const char *path, uint64_t sequence,
                            uint64_t loader, unsigned flags, int optimization);
int otelc_py_origin_cached_module(PyObject *code, const char *input_path, const char *source_path,
                                  uint64_t sequence, uint64_t loader);
int otelc_py_origin_local_callback(PyObject *code, int event, int events, PyObject *callback);
/* CodeCapture calls this only after authenticating its actual SDK start. */
int otelc_py_origin_sdk_join(PyObject *code, uint64_t sdk, int offset);

int otelc_py_origin_chain_ready(void);
int otelc_py_origin_chain_matches(void);

#endif
