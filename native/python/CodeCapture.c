/* Catalog real observed code, without Python callbacks inside the writer lock. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include <opcode.h>
#include "InputJournal.h"
#include "NativeEncoder.h"
#include "OriginEntry.h"

#include <stdint.h>
#include <limits.h>
#include <string.h>
#include <unistd.h>

#define CODE_LIMIT 4096U
#define NODE_LIMIT 65536U
#define DEPTH_LIMIT 128U

typedef struct { PyObject *weak; uint64_t identity; int failed; } CodeSlot;
static CodeSlot cache[CODE_LIMIT];
static PyInterpreterState *owner;
static pid_t owner_pid;
static PyObject *callback_code, *encoder, *encoder_code, *encoder_defaults;
static uint64_t next_identity;

static int same_owner(void) {
    return getpid() == owner_pid &&
        PyThreadState_GetInterpreter(PyThreadState_Get()) == owner;
}

static PyObject *failed(void) {
    otelc_py_journal_capture_failed();
    /* Capture must not swallow an application interrupt/exit. */
    if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt) || PyErr_ExceptionMatches(PyExc_SystemExit)) return NULL;
    PyErr_Clear();
    Py_RETURN_NONE;
}

static int budget(size_t *remaining, size_t amount) {
    if (amount > *remaining) return 0;
    *remaining -= amount;
    return 1;
}

static int safe_value(PyObject *value, size_t *remaining, size_t *nodes, unsigned depth);

static int safe_code(PyCodeObject *code, size_t *remaining, size_t *nodes, unsigned depth) {
    /* Both selected CPython header families store two-byte code units. Refuse
       before materializing a second bytecode buffer larger than the bound. */
    if (Py_SIZE(code) < 0 || (size_t)Py_SIZE(code) > *remaining / 2 || !budget(remaining, 512)) return 0;
    PyObject *fields[] = {code->co_consts, code->co_names, code->co_localsplusnames,
        code->co_localspluskinds, code->co_filename, code->co_name, code->co_qualname,
        code->co_linetable, code->co_exceptiontable};
    for (size_t index = 0; index < sizeof(fields) / sizeof(fields[0]); ++index) {
        if (!safe_value(fields[index], remaining, nodes, depth + 1)) return 0;
    }
    PyObject *bytes = PyCode_GetCode(code);
    if (!bytes) return 0;
    int result = safe_value(bytes, remaining, nodes, depth + 1);
    Py_DECREF(bytes);
    return result;
}

static int safe_value(PyObject *value, size_t *remaining, size_t *nodes, unsigned depth) {
    if (!value || depth > DEPTH_LIMIT || !*nodes || !budget(remaining, 256)) return 0;
    --*nodes;
    if (value == Py_None || value == Py_True || value == Py_False || value == Py_Ellipsis) return 1;
    if (PyCode_Check(value)) return safe_code((PyCodeObject *)value, remaining, nodes, depth);
    if (PyBytes_CheckExact(value)) {
        size_t length = (size_t)PyBytes_GET_SIZE(value);
        return length <= *remaining / 2 && budget(remaining, length * 2);
    }
    if (PyUnicode_CheckExact(value)) {
        size_t length = (size_t)PyUnicode_GET_LENGTH(value);
        return length <= *remaining / 12 && budget(remaining, length * 12);
    }
    if (PyLong_CheckExact(value)) {
        /* Exact immutable int: this builtin method cannot invoke an application
           attribute/formatter/buffer implementation. Allocation is outside locks. */
        PyObject *bits = PyObject_CallMethod(value, "bit_length", NULL);
        if (!bits) return 0;
        size_t size = PyLong_AsSize_t(bits);
        Py_DECREF(bits);
        if (PyErr_Occurred() || size / 8 > *remaining / 2) return 0;
        return budget(remaining, (size / 8 + 1) * 2);
    }
    if (PyFloat_CheckExact(value) || PyComplex_CheckExact(value)) return budget(remaining, 32);
    if (PyTuple_CheckExact(value)) {
        Py_ssize_t length = PyTuple_GET_SIZE(value);
        if ((size_t)length > *nodes) return 0;
        for (Py_ssize_t index = 0; index < length; ++index) {
            if (!safe_value(PyTuple_GET_ITEM(value, index), remaining, nodes, depth + 1)) return 0;
        }
        return 1;
    }
    if (PyFrozenSet_CheckExact(value)) {
        if ((size_t)PySet_GET_SIZE(value) > *nodes) return 0;
        PyObject *iterator = PyObject_GetIter(value);
        if (!iterator) return 0;
        PyObject *item;
        int result = 1;
        while ((item = PyIter_Next(iterator))) {
            result = safe_value(item, remaining, nodes, depth + 1);
            Py_DECREF(item);
            if (!result) break;
        }
        Py_DECREF(iterator);
        return result && !PyErr_Occurred();
    }
    if (Py_IS_TYPE(value, &PySlice_Type)) {
        PySliceObject *slice = (PySliceObject *)value;
        return safe_value(slice->start, remaining, nodes, depth + 1) &&
            safe_value(slice->stop, remaining, nodes, depth + 1) &&
            safe_value(slice->step, remaining, nodes, depth + 1);
    }
    /* Never allow generic buffer fallback or mutable/custom values. */
    return 0;
}

static int actual_start(PyObject *code, int offset) {
    PyFrameObject *frame = PyEval_GetFrame();
    if (!frame || !callback_code) return 0;
    PyCodeObject *caller = PyFrame_GetCode(frame);
    int matches = (PyObject *)caller == callback_code;
    Py_DECREF(caller);
    if (!matches) return 0;
    PyFrameObject *application = PyFrame_GetBack(frame);
    if (!application) return 0;
    PyCodeObject *actual = PyFrame_GetCode(application);
    matches = (PyObject *)actual == code && PyFrame_GetLasti(application) == offset;
    Py_DECREF(actual);
    Py_DECREF(application);
    if (!matches || offset < 0) return 0;
    /* The call site must be a real start/resume instruction, not a Python
       callback invocation inside the same function or an audit-event claim. */
    PyObject *bytes = PyCode_GetCode((PyCodeObject *)code);
    if (!bytes) return 0;
    matches = offset < PyBytes_GET_SIZE(bytes) &&
        (unsigned char)PyBytes_AS_STRING(bytes)[offset] == RESUME;
    Py_DECREF(bytes);
    return matches;
}

int otelc_py_code_safe(PyObject *code) {
    size_t remaining = OTELC_PY_SOURCE_LIMIT, nodes = NODE_LIMIT;
    return PyCode_Check(code) && safe_value(code, &remaining, &nodes, 0);
}

static PyObject *target(PyObject *weak) {
#if PY_VERSION_HEX >= 0x030D0000
    PyObject *value = NULL;
    if (PyWeakref_GetRef(weak, &value) < 0) return NULL;
    return value;
#else
    PyObject *value = PyWeakref_GetObject(weak);
    return value && value != Py_None ? Py_NewRef(value) : NULL;
#endif
}

static PyObject *identity_result(uint64_t identity) {
    if (!identity) Py_RETURN_NONE;
    PyObject *result = PyLong_FromUnsignedLongLong(identity);
    return result ? result : failed();
}

static int encoder_ready(PyObject *prepare) {
    if (!encoder || encoder != prepare || encoder_code != PyFunction_GetCode(prepare)) return 0;
    PyObject *defaults = PyFunction_GetKwDefaults(prepare);
    if (!defaults || !PyDict_CheckExact(defaults) ||
            PyDict_Size(defaults) != PyDict_Size(encoder_defaults)) return 0;
    Py_ssize_t position = 0;
    PyObject *key, *value;
    while (PyDict_Next(encoder_defaults, &position, &key, &value)) {
        if (PyDict_GetItemWithError(defaults, key) != value) return 0;
    }
    return 1;
}

static PyObject *bind_callback(PyObject *module, PyObject *arguments) {
    (void)module;
    if (!same_owner()) return failed();
    PyObject *callable, *prepare;
    if (!PyArg_UnpackTuple(arguments, "bind", 2, 2, &callable, &prepare)) return failed();
    PyObject *function = PyMethod_Check(callable) ? PyMethod_Function(callable) : callable;
    if (!PyFunction_Check(function) || !PyFunction_Check(prepare)) return failed();
    PyObject *code = PyFunction_GetCode(function);
    if (callback_code && (callback_code != code || !encoder_ready(prepare))) return failed();
    if (!callback_code) {
        PyObject *defaults = PyFunction_GetKwDefaults(prepare);
        if (!defaults || !PyDict_CheckExact(defaults)) return failed();
        PyObject *copy = PyDict_Copy(defaults);
        if (!copy) return failed();
        callback_code = Py_NewRef(code);
        encoder = Py_NewRef(prepare);
        encoder_code = Py_NewRef(PyFunction_GetCode(prepare));
        encoder_defaults = copy;
    }
    Py_RETURN_TRUE;
}

static PyObject *observe(PyObject *module, PyObject *arguments) {
    (void)module;
    PyObject *code, *position;
    if (!same_owner() || !PyArg_UnpackTuple(arguments, "observe", 2, 2, &code, &position)) return failed();
    if (!PyCode_Check(code) || !PyLong_CheckExact(position) ||
            Py_SIZE(code) < 0 || (size_t)Py_SIZE(code) > OTELC_PY_SOURCE_LIMIT / 2) return failed();
    long offset = PyLong_AsLong(position);
    if (PyErr_Occurred() || offset < 0 || offset > INT_MAX || !actual_start(code, (int)offset)) return failed();
    size_t available = CODE_LIMIT;
    for (size_t index = 0; index < CODE_LIMIT; ++index) {
        PyObject *existing = cache[index].weak ? target(cache[index].weak) : NULL;
        int match = existing == code;
        if (!existing && available == CODE_LIMIT) available = index;
        Py_XDECREF(existing);
        if (match) return identity_result(cache[index].failed ? 0 : cache[index].identity);
    }
    if (available == CODE_LIMIT) return failed();
    PyObject *weak = PyWeakref_NewRef(code, NULL);
    if (!weak) return failed();
    Py_XDECREF(cache[available].weak);
    cache[available].weak = weak;
    uint64_t identity = ++next_identity;
    cache[available].identity = identity;
    cache[available].failed = 0;
    size_t remaining = OTELC_PY_SOURCE_LIMIT, nodes = NODE_LIMIT;
    if (!identity || !safe_value(code, &remaining, &nodes, 0) ||
            !encoder_ready(encoder)) {
        cache[available].failed = 1;
        return failed();
    }
    PyObject *path = PyUnicode_EncodeFSDefault(((PyCodeObject *)code)->co_filename);
    /* CodeType.replace accepts arbitrary Unicode filenames. The raw writer
       takes a C path, so reject NUL/invalid lengths instead of retaining a prefix. */
    if (path && (!PyBytes_GET_SIZE(path) || PyBytes_GET_SIZE(path) > OTELC_PY_PATH_LIMIT ||
            memchr(PyBytes_AS_STRING(path), 0, (size_t)PyBytes_GET_SIZE(path)))) Py_CLEAR(path);
    PyObject *payload = path ? PyObject_CallOneArg(encoder, code) : NULL;
    if (!payload) {
        cache[available].failed = 1;
        Py_XDECREF(path);
        return failed();
    }
    if (!PyBytes_CheckExact(payload)) {
        cache[available].failed = 1;
        Py_DECREF(path);
        Py_DECREF(payload);
        return failed();
    }
    int result = otelc_py_journal_code(PyBytes_AS_STRING(path),
        (const unsigned char *)PyBytes_AS_STRING(payload), (size_t)PyBytes_GET_SIZE(payload),
        identity, (uint64_t)offset, PY_VERSION_HEX, 2);
    Py_DECREF(path);
    Py_DECREF(payload);
    if (result) {
        cache[available].failed = 1;
        Py_RETURN_NONE; /* Writer already records its admission/IO loss. */
    }
    if (otelc_py_origin_sdk_join(code, identity, (int)offset)) {
        cache[available].failed = 1;
        if (PyErr_Occurred()) return NULL;
        Py_RETURN_NONE; /* Origin capture retains its own failure evidence. */
    }
    return identity_result(identity);
}

static PyObject *native_encode(PyObject *module, PyObject *code) {
    (void)module;
    return otelc_py_native_encode(code);
}

static PyMethodDef methods[] = {
    {"bind", bind_callback, METH_VARARGS, NULL},
    {"observe", observe, METH_VARARGS, NULL},
    {"encode", native_encode, METH_O, NULL},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef definition = {
    PyModuleDef_HEAD_INIT, "_otelc_inputs", NULL, -1, methods, NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC PyInit__otelc_inputs(void) {
    if (owner && !same_owner()) {
        PyErr_SetString(PyExc_ImportError, "native code observation supports one owning interpreter");
        return NULL;
    }
    owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    owner_pid = getpid();
    return PyModule_Create(&definition);
}

int otelc_py_code_install(void) {
    return PyImport_AppendInittab("_otelc_inputs", PyInit__otelc_inputs);
}
