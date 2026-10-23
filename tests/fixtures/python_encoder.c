/* Native API failure boundaries, without adding testing controls to the product. */
#include <Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail_function;
static int fail_configuration;
static PyObject *fixture_function_new(PyObject *code, PyObject *globals) {
    if (fail_function) return PyErr_NoMemory();
    return PyFunction_New(code, globals);
}

#define PyFunction_New fixture_function_new
#if PY_VERSION_HEX >= 0x030E0000
static PyObject *fixture_config_get(const char *name) {
    return fail_configuration ? PyErr_NoMemory() : PyConfig_Get(name);
}
#define PyConfig_Get fixture_config_get
#else
static PyObject *fixture_unicode(const wchar_t *value, Py_ssize_t length) {
    return fail_configuration ? PyErr_NoMemory() : PyUnicode_FromWideChar(value, length);
}
#define PyUnicode_FromWideChar fixture_unicode
#endif
#include "NativeEncoder.c"
#undef PyFunction_New
#if PY_VERSION_HEX >= 0x030E0000
#undef PyConfig_Get
#else
#undef PyUnicode_FromWideChar
#endif

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.site_import = 0;
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, argv[2]);
    if (!PyStatus_Exception(status)) status = PyConfig_Read(&config);
    if (!PyStatus_Exception(status) && !strcmp(argv[1], "filesystem")) {
        status = PyConfig_SetString(&config, &config.filesystem_encoding, L"UTF-8");
    }
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
        fail_configuration = 1;
        result = otelc_py_encoder_ready() < 0 && PyErr_ExceptionMatches(PyExc_MemoryError) ? 0 : 1;
        PyErr_Clear();
        if (otelc_py_encoder_ready() == 0 ||
                !PyErr_ExceptionMatches(PyExc_RuntimeError)) result = 1;
    } else {
        fail_function = !strcmp(mode, "allocation");
        int ready = otelc_py_encoder_ready();
        if (fail_function) result = ready < 0 && PyErr_ExceptionMatches(PyExc_MemoryError) ? 0 : 1;
        else if (!ready && !strcmp(mode, "repeat")) {
            result = otelc_py_encoder_ready();
        } else if (!ready && !strcmp(mode, "filesystem")) {
            if (!PyRun_SimpleString("def workload(): return 42\n")) {
                PyObject *function = PyDict_GetItemString(PyModule_GetDict(PyImport_AddModule("__main__")), "workload");
                PyObject *encoded = otelc_py_native_encode(PyFunction_GetCode(function));
                result = encoded && !wcscmp(config.filesystem_encoding, L"UTF-8") &&
                    strstr(PyBytes_AsString(encoded), "\"encoding\":\"utf-8\"") ? 0 : 1;
                Py_XDECREF(encoded);
            }
        } else if (!ready && !strcmp(mode, "interpreter")) {
            PyThreadState *main = PyThreadState_Get();
            PyThreadState *other = Py_NewInterpreter();
            if (other) {
                int refused = otelc_py_encoder_ready() < 0;
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
