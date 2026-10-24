#ifndef OTELC_PYTHON_ORIGIN_CACHE_H
#define OTELC_PYTHON_ORIGIN_CACHE_H
#include <Python.h>

int otelc_py_origin_cache_ready(void);
int otelc_py_origin_cache_active(void);
void otelc_py_origin_cache_cancel(void);
#endif
