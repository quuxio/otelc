#ifndef OTELC_NATIVE_ENCODER_H
#define OTELC_NATIVE_ENCODER_H

#include <Python.h>

int otelc_py_code_safe(PyObject *code);
int otelc_py_encoder_ready(void);
PyObject *otelc_py_native_encode(PyObject *code);

#endif
