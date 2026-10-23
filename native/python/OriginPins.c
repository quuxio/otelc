/* Retain frozen loader/compiler identities before application startup hooks. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include "OriginPins.h"

#include <string.h>
#include <unistd.h>

#define HELPER_COUNT 5U
#define FUNCTION_COUNT 4U

static PyObject *functions[HELPER_COUNT], *codes[FUNCTION_COUNT];
static PyInterpreterState *owner;
static pid_t owner_pid;
static int installed;

static PyObject *module_member(PyObject *module, const char *name) {
    return module && PyModule_CheckExact(module)
        ? PyDict_GetItemString(PyModule_GetDict(module), name) : NULL;
}

static PyObject *method(PyObject *module, const char *class_name, const char *name) {
    PyObject *type = module_member(module, class_name);
    return type && PyType_Check(type)
        ? PyDict_GetItemString(((PyTypeObject *)type)->tp_dict, name) : NULL;
}

static int current(PyObject *values[HELPER_COUNT]) {
    PyObject *modules = PyImport_GetModuleDict();
    PyObject *external = PyDict_GetItemString(modules, "_frozen_importlib_external");
    PyObject *internal = PyDict_GetItemString(modules, "_frozen_importlib");
    PyObject *builtins = PyDict_GetItemString(modules, "builtins");
    values[0] = method(external, "SourceLoader", "get_code");
    values[1] = method(external, "SourceLoader", "source_to_code");
    values[2] = method(external, "FileLoader", "get_data");
    values[3] = module_member(internal, "_call_with_frames_removed");
    values[4] = module_member(builtins, "compile");
    for (unsigned index = 0; index < FUNCTION_COUNT; ++index) {
        if (!values[index] || !PyFunction_Check(values[index])) return 0;
    }
    return values[4] && PyCFunction_Check(values[4]);
}

static int audit(const char *event, PyObject *arguments, void *unused) {
    (void)arguments;
    (void)unused;
    /* A single owning interpreter retains bounded toolchain references. Never
       dereference them from a fork or a different interpreter. */
    if (strcmp(event, "import") || owner || getpid() != owner_pid) return 0;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *values[HELPER_COUNT];
    if (current(values)) {
        for (unsigned index = 0; index < HELPER_COUNT; ++index) functions[index] = Py_NewRef(values[index]);
        for (unsigned index = 0; index < FUNCTION_COUNT; ++index) {
            codes[index] = Py_NewRef(PyFunction_GetCode(values[index]));
        }
        owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    }
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    return 0;
}

int otelc_py_origin_pins_install(void) {
    if (installed || Py_IsInitialized()) return -1;
    owner_pid = getpid();
    if (PySys_AddAuditHook(audit, NULL)) return -1;
    installed = 1;
    return 0;
}

int otelc_py_origin_pins_ready(void) {
    if (!owner || getpid() != owner_pid ||
            PyThreadState_GetInterpreter(PyThreadState_Get()) != owner) return -1;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *values[HELPER_COUNT];
    int valid = current(values);
    for (unsigned index = 0; valid && index < HELPER_COUNT; ++index) {
        valid = values[index] == functions[index];
        if (valid && index < FUNCTION_COUNT) valid = PyFunction_GetCode(values[index]) == codes[index];
    }
    /* Do not leak lookup errors into an application exception in flight. */
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    return valid ? 0 : -1;
}
