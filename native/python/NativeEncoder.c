/* Matched-CPython helper, with no application import or SDK dependency. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif
#include <Python.h>
#include "InputJournal.h"
#include "NativeEncoder.h"

#include <dlfcn.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static PyInterpreterState *encoder_owner;
static pid_t encoder_pid;
static PyObject *encoder_function, *encoder_code;
static void *json_handle, *struct_handle;
static int attempted;

static PyObject *native_getattr(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *value, *name;
    if (!PyArg_UnpackTuple(args, "getattr", 2, 2, &value, &name)) return NULL;
    return PyObject_GetAttr(value, name);
}

static PyObject *native_len(PyObject *self, PyObject *value) {
    (void)self;
    Py_ssize_t length = PyObject_Length(value);
    return length < 0 ? NULL : PyLong_FromSsize_t(length);
}

static PyObject *native_constant(PyObject *value, PyObject *unused) {
    (void)unused;
    return Py_NewRef(value);
}

static PyMethodDef native_getattr_method = {"getattr", native_getattr, METH_VARARGS, NULL};
static PyMethodDef native_len_method = {"len", native_len, METH_O, NULL};
static PyMethodDef native_constant_method = {"filesystem", native_constant, METH_NOARGS, NULL};

static PyObject *native_callable(PyMethodDef *definition, PyObject *self) {
    return PyCFunction_NewEx(definition, self, NULL);
}

#include "EncoderFactory.h"

/* Keep the fixed reference set outside the mutable Python object graph.
   Copying a dict does not protect it: both copies are visible through GC. */
static struct { PyObject *name, *value; } encoder_defaults[OTELC_ENCODER_DEFAULT_COUNT];

static PyObject *private_extension(const char *path, const char *symbol,
                                   const char *name, void **handle) {
    *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    if (!*handle) {
        PyErr_SetString(PyExc_ImportError, "pinned native encoder dependency cannot load");
        return NULL;
    }
    /* Keep handles until process exit, including failed initialization: a
       deferred GC cycle may still own a type/function from this library. */
    void *address = dlsym(*handle, symbol);
    Dl_info location;
    char actual[PATH_MAX];
    if (!address || !dladdr(address, &location) || !location.dli_fname ||
            !realpath(location.dli_fname, actual) || strcmp(path, actual)) {
        PyErr_SetString(PyExc_ImportError, "native encoder dependency origin differs");
        return NULL;
    }
    PyObject *(*initialize)(void);
    _Static_assert(sizeof(address) == sizeof(initialize), "unsupported POSIX function pointer ABI");
    memcpy(&initialize, &address, sizeof(address));
    PyObject *definition = initialize();
    if (!definition) return NULL;
    if (!Py_IS_TYPE(definition, &PyModuleDef_Type)) {
        Py_DECREF(definition);
        PyErr_SetString(PyExc_ImportError, "native encoder requires multi-phase standard extensions");
        return NULL;
    }
    PyObject *spec = PyModule_New(name);
    PyObject *label = PyUnicode_FromString(name);
    if (!spec || !label || PyObject_SetAttrString(spec, "name", label)) {
        Py_XDECREF(spec);
        Py_XDECREF(label);
        return NULL;
    }
    Py_DECREF(label);
    PyObject *module = PyModule_FromDefAndSpec((PyModuleDef *)definition, spec);
    Py_DECREF(spec);
    if (module && PyModule_ExecDef(module, (PyModuleDef *)definition)) Py_CLEAR(module);
    return module;
}

static int same_encoder_owner(void) {
    return encoder_owner && getpid() == encoder_pid &&
        PyThreadState_GetInterpreter(PyThreadState_Get()) == encoder_owner;
}

int otelc_py_encoder_ready(const wchar_t *encoding, const wchar_t *errors) {
    if (attempted) {
        if (same_encoder_owner() && encoder_function) return 0;
        PyErr_SetString(PyExc_RuntimeError, "native encoder is unavailable for this process/interpreter");
        return -1;
    }
    attempted = 1;
    encoder_owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    encoder_pid = getpid();
    PyObject *json = NULL, *structure = NULL, *globals = NULL, *code = NULL;
    PyObject *fs_encoding = NULL, *fs_errors = NULL, *builtins = NULL, *name = NULL;
    PyObject *function = NULL, *defaults = NULL;
    if (!encoding || !errors) {
        PyErr_SetString(PyExc_ValueError, "native encoder filesystem configuration is unavailable");
        return -1;
    }
    json = private_extension(OTELC_ENCODER_JSON_PATH, "PyInit__json", "otelc.native._json", &json_handle);
    if (!json) goto failed;
    structure = private_extension(OTELC_ENCODER_STRUCT_PATH, "PyInit__struct", "otelc.native._struct", &struct_handle);
    if (!structure) goto failed;
    fs_encoding = PyUnicode_FromWideChar(encoding, -1);
    fs_errors = PyUnicode_FromWideChar(errors, -1);
    globals = PyDict_New();
    builtins = PyDict_New();
    name = PyUnicode_FromString("otelc.native.encoder");
    code = encoder_code_factory();
    if (!fs_encoding || !fs_errors || !globals || !builtins || !name || !code ||
            PyDict_SetItemString(globals, "__builtins__", builtins) ||
            PyDict_SetItemString(globals, "__name__", name)) goto failed;
    function = PyFunction_New(code, globals);
    if (!function || encoder_defaults_factory(function, json, structure, fs_encoding, fs_errors)) goto failed;
    defaults = PyFunction_GetKwDefaults(function); /* Borrowed from our function. */
    if (!defaults || !PyDict_CheckExact(defaults) ||
            PyDict_Size(defaults) != OTELC_ENCODER_DEFAULT_COUNT) {
        PyErr_SetString(PyExc_RuntimeError, "native encoder default definition differs");
        goto failed;
    }
    Py_ssize_t position = 0;
    size_t index = 0;
    PyObject *key, *value;
    while (PyDict_Next(defaults, &position, &key, &value)) {
        encoder_defaults[index].name = Py_NewRef(key);
        encoder_defaults[index++].value = Py_NewRef(value);
    }
    encoder_function = function;
    encoder_code = Py_NewRef(code);
    Py_DECREF(json); Py_DECREF(structure); Py_DECREF(globals); Py_DECREF(code);
    Py_DECREF(fs_encoding); Py_DECREF(fs_errors); Py_DECREF(builtins); Py_DECREF(name);
    return 0;
failed:
    Py_XDECREF(json); Py_XDECREF(structure); Py_XDECREF(globals); Py_XDECREF(code);
    Py_XDECREF(fs_encoding); Py_XDECREF(fs_errors); Py_XDECREF(builtins); Py_XDECREF(name);
    Py_XDECREF(function);
    for (size_t index = 0; index < OTELC_ENCODER_DEFAULT_COUNT; ++index) {
        Py_CLEAR(encoder_defaults[index].name);
        Py_CLEAR(encoder_defaults[index].value);
    }
    return -1;
}

PyObject *otelc_py_native_encode(PyObject *code) {
    if (!same_encoder_owner() || !encoder_function) {
        PyErr_SetString(PyExc_RuntimeError, "native encoder is not installed for this process/interpreter");
        return NULL;
    }
    PyObject *defaults = PyFunction_GetKwDefaults(encoder_function);
    if (PyFunction_GetCode(encoder_function) != encoder_code || !defaults ||
            !PyDict_CheckExact(defaults) || PyDict_Size(defaults) != OTELC_ENCODER_DEFAULT_COUNT) {
        PyErr_SetString(PyExc_RuntimeError, "native encoder function/defaults changed");
        return NULL;
    }
    Py_ssize_t position = 0;
    PyObject *key, *value;
    while (PyDict_Next(defaults, &position, &key, &value)) {
        if (!PyUnicode_CheckExact(key)) {
            PyErr_SetString(PyExc_RuntimeError, "native encoder defaults changed");
            return NULL;
        }
    }
    for (size_t index = 0; index < OTELC_ENCODER_DEFAULT_COUNT; ++index) {
        if (PyDict_GetItemWithError(defaults, encoder_defaults[index].name) != encoder_defaults[index].value) {
            PyErr_SetString(PyExc_RuntimeError, "native encoder defaults changed");
            return NULL;
        }
    }
    if (!otelc_py_code_safe(code)) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "unsupported or oversized code graph");
        return NULL;
    }
    PyObject *result = PyObject_CallOneArg(encoder_function, code);
    if (result && (!PyBytes_CheckExact(result) || PyBytes_GET_SIZE(result) > OTELC_PY_SOURCE_LIMIT)) {
        Py_DECREF(result);
        PyErr_SetString(PyExc_ValueError, "native encoder result exceeds its format/bound");
        return NULL;
    }
    return result;
}
