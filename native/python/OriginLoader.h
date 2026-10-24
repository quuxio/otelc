#ifndef OTELC_PYTHON_ORIGIN_LOADER_H
#define OTELC_PYTHON_ORIGIN_LOADER_H

#include <Python.h>
#include <stdint.h>

int otelc_py_origin_loader_ready(void);
int otelc_py_origin_loader_root(const char *path);
int otelc_py_origin_loader_active(void);
int otelc_py_origin_loader_read(PyObject *path, PyObject *bytes, uint64_t sequence);
int otelc_py_origin_loader_start(PyObject *code);
void otelc_py_origin_loader_cancel(void);
int otelc_py_origin_loader_cache_input(PyObject *data);
int otelc_py_origin_loader_cache_return(PyObject *data, PyObject *code);

#endif
