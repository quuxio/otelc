/* Native API failure boundaries, without adding testing controls to the product. */
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail_function;
static PyObject *fixture_function_new(PyObject *code, PyObject *globals) {
    if (fail_function) return PyErr_NoMemory();
    return PyFunction_New(code, globals);
}

#define PyFunction_New fixture_function_new
#include "NativeEncoder.c"
#undef PyFunction_New

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.site_import = 0;
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, argv[2]);
    if (!PyStatus_Exception(status)) status = PyConfig_Read(&config);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) { PyConfig_Clear(&config); return 65; }
    const char *mode = argv[1];
    int result = 1;
    if (!strcmp(mode, "missing") || !strcmp(mode, "symbol") || !strcmp(mode, "origin")) {
        void *handle = NULL;
        const char *path = !strcmp(mode, "missing") ? "/otelc-nonexistent/dependency" : OTELC_ENCODER_JSON_PATH;
        char alias[PATH_MAX];
        if (!strcmp(mode, "origin")) {
            if (snprintf(alias, sizeof(alias), "/.%s", path) >= (int)sizeof(alias)) return 66;
            path = alias;
        }
        const char *symbol = !strcmp(mode, "symbol") ? "PyInit_otelc_missing" : "PyInit__json";
        PyObject *module = private_extension(path, symbol, "fixture", &handle);
        result = module == NULL && PyErr_ExceptionMatches(PyExc_ImportError) ? 0 : 1;
        Py_XDECREF(module);
    } else if (!strcmp(mode, "configuration")) {
        result = otelc_py_encoder_ready(NULL, NULL) < 0 && PyErr_ExceptionMatches(PyExc_ValueError) ? 0 : 1;
        PyErr_Clear();
        if (otelc_py_encoder_ready(config.filesystem_encoding, config.filesystem_errors) == 0 ||
                !PyErr_ExceptionMatches(PyExc_RuntimeError)) result = 1;
    } else {
        fail_function = !strcmp(mode, "allocation");
        int ready = otelc_py_encoder_ready(config.filesystem_encoding, config.filesystem_errors);
        if (fail_function) result = ready < 0 && PyErr_ExceptionMatches(PyExc_MemoryError) ? 0 : 1;
        else if (!ready && !strcmp(mode, "repeat")) {
            result = otelc_py_encoder_ready(config.filesystem_encoding, config.filesystem_errors);
        } else if (!ready && !strcmp(mode, "interpreter")) {
            PyThreadState *main = PyThreadState_Get();
            PyThreadState *other = Py_NewInterpreter();
            if (other) {
                int refused = otelc_py_encoder_ready(config.filesystem_encoding, config.filesystem_errors) < 0;
                PyErr_Clear();
                PyObject *encoded = otelc_py_native_encode(Py_None);
                result = refused && !encoded && PyErr_ExceptionMatches(PyExc_RuntimeError) ? 0 : 1;
                Py_XDECREF(encoded);
                PyErr_Clear();
                Py_EndInterpreter(other);
                PyThreadState_Swap(main);
            }
        }
    }
    PyErr_Clear();
    PyConfig_Clear(&config);
    if (Py_FinalizeEx() < 0) return 67;
    return result;
}
