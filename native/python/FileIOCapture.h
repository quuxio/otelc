#ifndef OTELC_PYTHON_FILEIO_CAPTURE_H
#define OTELC_PYTHON_FILEIO_CAPTURE_H

#include <Python.h>

int otelc_py_fileio_install(PyObject *type, PyObject *loader_code);
PyFrameObject *otelc_py_fileio_suppress(PyFrameObject *frame);

#endif
