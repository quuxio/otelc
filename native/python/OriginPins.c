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
static int monitor_requested;
static PyObject *monitor_functions[OTELC_MONITOR_METHODS];
static const char *monitor_names[] = {
    "get_tool", "use_tool_id", "register_callback", "set_local_events", "free_tool_id", "DISABLE"
};

static PyObject *named_value(PyObject *dictionary, const char *name) {
    /* Hash lookups can invoke a custom key's __eq__, even with a literal
       string lookup. These dictionaries remain application-modifiable. */
    if (!dictionary || !PyDict_CheckExact(dictionary)) return NULL;
    Py_ssize_t position = 0;
    PyObject *key, *value;
    while (PyDict_Next(dictionary, &position, &key, &value)) {
        if (PyUnicode_CheckExact(key) && PyUnicode_CompareWithASCIIString(key, name) == 0) return value;
    }
    return NULL;
}

static PyObject *module_member(PyObject *module, const char *name) {
    return module && PyModule_CheckExact(module)
        ? named_value(PyModule_GetDict(module), name) : NULL;
}

static PyObject *method(PyObject *module, const char *class_name, const char *name) {
    PyObject *type = module_member(module, class_name);
    return type && PyType_Check(type)
        ? named_value(((PyTypeObject *)type)->tp_dict, name) : NULL;
}

static int current(PyObject *values[HELPER_COUNT]) {
    PyObject *modules = PyImport_GetModuleDict();
    PyObject *external = named_value(modules, "_frozen_importlib_external");
    PyObject *internal = named_value(modules, "_frozen_importlib");
    PyObject *builtins = named_value(modules, "builtins");
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

static int monitor_current(PyObject *values[OTELC_MONITOR_METHODS]) {
    PyObject *sys = named_value(PyImport_GetModuleDict(), "sys");
    PyObject *monitor = module_member(sys, "monitoring");
    for (unsigned index = 0; index < OTELC_MONITOR_METHODS; ++index) {
        values[index] = module_member(monitor, monitor_names[index]);
        if (!values[index] || (index != OTELC_MONITOR_DISABLE && !PyCFunction_Check(values[index]))) return 0;
    }
    return 1;
}

static int audit(const char *event, PyObject *arguments, void *unused) {
    (void)arguments;
    (void)unused;
    /* A single owning interpreter retains bounded toolchain references. Never
       dereference them from a fork or a different interpreter. */
    if (strcmp(event, "import") || owner || getpid() != owner_pid) return 0;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *values[HELPER_COUNT];
    PyObject *monitor[OTELC_MONITOR_METHODS];
    if (current(values) && (!monitor_requested || monitor_current(monitor))) {
        for (unsigned index = 0; index < HELPER_COUNT; ++index) functions[index] = Py_NewRef(values[index]);
        for (unsigned index = 0; index < FUNCTION_COUNT; ++index) {
            codes[index] = Py_NewRef(PyFunction_GetCode(values[index]));
        }
        if (monitor_requested) {
            for (unsigned index = 0; index < OTELC_MONITOR_METHODS; ++index) {
                monitor_functions[index] = Py_NewRef(monitor[index]);
            }
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
    if (valid && monitor_requested) {
        PyObject *monitor[OTELC_MONITOR_METHODS];
        valid = monitor_current(monitor);
        for (unsigned index = 0; valid && index < OTELC_MONITOR_METHODS; ++index) {
            valid = monitor[index] == monitor_functions[index];
        }
    }
    /* Do not leak lookup errors into an application exception in flight. */
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    return valid ? 0 : -1;
}

int otelc_py_origin_monitor_enable(void) {
    if (monitor_requested || installed || Py_IsInitialized()) return -1;
    monitor_requested = 1;
    return 0;
}

PyObject *otelc_py_origin_monitor_value(unsigned index) {
    if (!monitor_requested || index >= OTELC_MONITOR_METHODS || otelc_py_origin_pins_ready()) return NULL;
    return monitor_functions[index];
}
