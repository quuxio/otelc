/* Bind an actual ordinary source read to its compiler and loader returns. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include <opcode.h>
#include "InputJournal.h"
#include "OriginEntry.h"
#include "OriginPins.h"
#include "OriginLoader.h"
#include "OriginChain.h"

#if OTELC_ORIGIN_CHAIN_AVAILABLE
#include <string.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

extern const PyConfig *_Py_GetConfig(void);
#define PENDING_LIMIT 32U
#define LOADER_LIMIT 4096U
#define RETURN_EVENT 4

typedef struct {
    PyFrameObject *frame;
    PyObject *bytes, *path, *root;
    uint64_t sequence, loader;
    unsigned long thread;
    int compiled, optimization;
} Pending;
typedef struct { PyObject *weak; uint64_t identity; } Loader;
static Pending pending[PENDING_LIMIT];
static Loader loaders[LOADER_LIMIT];
static uint64_t next_loader;
static PyObject *compiler, *helpers[4], *loader_type;
static vectorcallfunc original_compile;
static PyInterpreterState *owner;
static pid_t owner_pid;
static int active;
static char *application_root;
static PyObject *compile_call(PyObject *callable, PyObject *const *args, size_t count, PyObject *names);

int otelc_py_origin_loader_root(const char *path) {
    if (active || owner || application_root || !path || path[0] != '/' ||
            strlen(path) > OTELC_PY_PATH_LIMIT) return -1;
    struct stat info;
    if (stat(path, &info) || !S_ISDIR(info.st_mode)) return -1;
    application_root = realpath(path, NULL);
    return application_root ? 0 : -1;
}

static int in_scope(PyFrameObject *frame) {
    if (!application_root) return 1;
    /* Read the pinned get_code frame's actual source_path, without calling
       loader properties or application path conversion methods. */
    PyObject *source = PyFrame_GetVarString(frame, "source_path");
    PyObject *bytes = source && PyUnicode_CheckExact(source) ? PyUnicode_EncodeFSDefault(source) : NULL;
    Py_XDECREF(source);
    if (!bytes) return -1;
    Py_ssize_t size = PyBytes_GET_SIZE(bytes);
    int valid = size > 0 && size <= OTELC_PY_PATH_LIMIT &&
        !memchr(PyBytes_AS_STRING(bytes), 0, (size_t)size);
    char *canonical = valid ? realpath(PyBytes_AS_STRING(bytes), NULL) : NULL;
    Py_DECREF(bytes);
    if (!canonical) return -1;
    size_t length = strlen(application_root);
    int inside = !strcmp(application_root, "/") ||
        (!strncmp(canonical, application_root, length) && canonical[length] == '/');
    free(canonical);
    return inside;
}

static int same_owner(void) {
    return active && getpid() == owner_pid &&
        PyThreadState_GetInterpreter(PyThreadState_Get()) == owner;
}

static void clear(Pending *slot) {
    /* The frame is an opaque borrowed address, never dereferenced or retained.
       Genuine local starts invalidate reused addresses before accepting reads. */
    slot->frame = NULL;
    Py_CLEAR(slot->bytes); Py_CLEAR(slot->path); Py_CLEAR(slot->root);
    slot->sequence = slot->loader = 0;
    slot->compiled = slot->optimization = 0;
}

void otelc_py_origin_loader_cancel(void) {
    if (!active) return;
    active = 0;
    if (compiler && ((PyCFunctionObject *)compiler)->vectorcall == compile_call) {
        ((PyCFunctionObject *)compiler)->vectorcall = original_compile;
    }
    for (size_t index = 0; index < PENDING_LIMIT; ++index) clear(&pending[index]);
}

static int fail(PyObject *saved, int counted) {
    if (!counted) otelc_py_journal_capture_failed();
    otelc_py_origin_loader_cancel();
    otelc_py_origin_entry_cancel();
    if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt) || PyErr_ExceptionMatches(PyExc_SystemExit)) {
        Py_XDECREF(saved);
        return -1;
    }
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    return 0;
}

static PyObject *referent(PyObject *weak) {
#if PY_VERSION_HEX >= 0x030D0000
    PyObject *value = NULL;
    if (PyWeakref_GetRef(weak, &value) < 0) return NULL;
    return value;
#else
    PyObject *value = PyWeakref_GetObject(weak);
    return value && value != Py_None ? Py_NewRef(value) : NULL;
#endif
}

static int frame_is(PyFrameObject *frame, PyObject *code) {
    PyCodeObject *actual = frame ? PyFrame_GetCode(frame) : NULL;
    int matches = actual && (PyObject *)actual == code;
    Py_XDECREF(actual);
    return matches;
}

static Pending *find(PyFrameObject *frame) {
    for (size_t index = 0; index < PENDING_LIMIT; ++index) {
        if (pending[index].frame == frame) return &pending[index];
    }
    return NULL;
}

int otelc_py_origin_loader_start(PyObject *code) {
    if (!same_owner() || code != helpers[0]) return 0;
    unsigned long thread = PyThread_get_thread_ident();
    PyFrameObject *current = PyEval_GetFrame();
    PyFrameObject *frames[128];
    size_t count = 0;
    PyFrameObject *frame = current ? (PyFrameObject *)Py_NewRef((PyObject *)current) : NULL;
    while (frame && count < 128) {
        frames[count++] = frame;
        frame = PyFrame_GetBack(frame);
    }
    int complete = frame == NULL;
    Py_XDECREF(frame);
    if (complete) {
        for (size_t index = 0; index < PENDING_LIMIT; ++index) {
            Pending *slot = &pending[index];
            if (!slot->frame || slot->thread != thread) continue;
            int live = 0;
            for (size_t depth = 1; depth < count; ++depth) live |= slot->frame == frames[depth];
            if (!live) clear(slot);
        }
    }
    for (size_t index = 0; index < count; ++index) Py_DECREF(frames[index]);
    if (!current || find(current)) return -1;
    for (size_t index = 0; index < PENDING_LIMIT; ++index) {
        if (!pending[index].frame) {
            pending[index].frame = current;
            pending[index].thread = thread;
            return 1;
        }
    }
    return -1;
}

static uint64_t loader_id(PyObject *value) {
    Loader *available = NULL;
    for (size_t index = 0; index < LOADER_LIMIT; ++index) {
        PyObject *object = loaders[index].weak ? referent(loaders[index].weak) : NULL;
        int same = object == value;
        if (!object && !available) available = &loaders[index];
        Py_XDECREF(object);
        if (same) return loaders[index].identity;
    }
    if (!available || next_loader == UINT64_MAX) return 0;
    PyObject *weak = PyWeakref_NewRef(value, NULL);
    if (!weak) return 0;
    Py_XSETREF(available->weak, weak);
    return available->identity = ++next_loader;
}

int otelc_py_origin_loader_read(PyObject *path, PyObject *bytes, uint64_t sequence) {
    if (!same_owner()) return 0;
    PyObject *saved = PyErr_GetRaisedException();
    PyFrameObject *data = PyEval_GetFrame();
    PyFrameObject *frame = data ? PyFrame_GetBack(data) : NULL;
    if (!frame_is(data, helpers[2]) || !frame_is(frame, helpers[0])) {
        Py_XDECREF(frame);
        PyErr_SetRaisedException(saved);
        return 0;
    }
    int scope = in_scope(frame);
    if (scope < 0) { Py_DECREF(frame); return fail(saved, 0); }
    if (!scope) { Py_DECREF(frame); PyErr_SetRaisedException(saved); return 0; }
    int source = PyBytes_CheckExact(path) && PyBytes_GET_SIZE(path) >= 3 &&
        !memcmp(PyBytes_AS_STRING(path) + PyBytes_GET_SIZE(path) - 3, ".py", 3);
    if (!source) { Py_DECREF(frame); PyErr_SetRaisedException(saved); return 0; }
    Pending *slot = find(frame);
    PyObject *self = PyFrame_GetVarString(frame, "self");
    int valid = sequence && PyBytes_CheckExact(bytes) && slot && !slot->bytes && self &&
        (PyObject *)Py_TYPE(self) == loader_type && otelc_py_origin_loader_type() == loader_type;
    uint64_t identity = valid ? loader_id(self) : 0;
    Py_XDECREF(self);
    if (!valid || !identity) {
        Py_DECREF(frame);
        return fail(saved, !sequence);
    }
    Py_DECREF(frame);
    slot->bytes = Py_NewRef(bytes);
    slot->path = Py_NewRef(path);
    slot->sequence = sequence;
    slot->loader = identity;
    PyErr_SetRaisedException(saved);
    return 0;
}

static int arguments_match(Pending *slot, PyObject *const *args, size_t count, PyObject *names) {
    if (PyVectorcall_NARGS(count) != 3 || args[0] != slot->bytes || !PyUnicode_CheckExact(args[1]) ||
            !PyUnicode_CheckExact(args[2]) || PyUnicode_CompareWithASCIIString(args[2], "exec") ||
            !names || !PyTuple_CheckExact(names) || PyTuple_GET_SIZE(names) != 2) return 0;
    int inherit = 0, optimize = 0;
    for (Py_ssize_t index = 0; index < 2; ++index) {
        PyObject *key = PyTuple_GET_ITEM(names, index), *value = args[3 + index];
        if (!PyUnicode_CheckExact(key)) return 0;
        if (!PyUnicode_CompareWithASCIIString(key, "dont_inherit") && value == Py_True) ++inherit;
        else if (!PyUnicode_CompareWithASCIIString(key, "optimize") && PyLong_CheckExact(value)) {
            long level = PyLong_AsLong(value);
            if (PyErr_Occurred() || level < -1 || level > 2) return 0;
            slot->optimization = level == -1 ? _Py_GetConfig()->optimization_level : (int)level;
            ++optimize;
        } else return 0;
    }
    PyObject *path = PyUnicode_EncodeFSDefault(args[1]);
    int same = path && PyBytes_GET_SIZE(path) == PyBytes_GET_SIZE(slot->path) &&
        !memcmp(PyBytes_AS_STRING(path), PyBytes_AS_STRING(slot->path), (size_t)PyBytes_GET_SIZE(path));
    Py_XDECREF(path);
    return same && inherit == 1 && optimize == 1;
}

static PyObject *compile_call(PyObject *callable, PyObject *const *args, size_t count, PyObject *names) {
    if (!same_owner()) return original_compile(callable, args, count, names);
    PyFrameObject *helper = PyEval_GetFrame();
    PyFrameObject *source = helper ? PyFrame_GetBack(helper) : NULL;
    PyFrameObject *frame = source ? PyFrame_GetBack(source) : NULL;
    int context = frame_is(helper, helpers[3]) && frame_is(source, helpers[1]) && frame_is(frame, helpers[0]);
    Pending *slot = context ? find(frame) : NULL;
    PyObject *saved = PyErr_GetRaisedException();
    int scope = context ? in_scope(frame) : 1;
    Py_XDECREF(source); Py_XDECREF(frame);
    if (!context || !scope) {
        PyErr_SetRaisedException(saved);
        return original_compile(callable, args, count, names);
    }
    if (scope < 0 || !slot || slot->compiled || otelc_py_origin_pins_ready() || !arguments_match(slot, args, count, names)) {
        if (fail(saved, 0)) return NULL;
        return original_compile(callable, args, count, names);
    }
    PyErr_SetRaisedException(saved);
    PyObject *result = original_compile(callable, args, count, names);
    if (!same_owner()) return result;
    slot->compiled = 1;
    if (!result) { clear(slot); return NULL; }
    saved = PyErr_GetRaisedException();
    if (!PyCode_Check(result) || !(slot->root = PyWeakref_NewRef(result, NULL))) {
        if (fail(saved, 0)) { Py_DECREF(result); return NULL; }
        return result;
    }
    PyErr_SetRaisedException(saved);
    return result;
}

static PyObject *returned(PyObject *unwind, PyObject *arguments) {
    (void)unwind;
    if (!same_owner()) Py_RETURN_NONE;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *code, *offset, *result;
    PyFrameObject *frame = PyEval_GetFrame();
    if (!PyArg_UnpackTuple(arguments, "loader_return", 3, 3, &code, &offset, &result) ||
            code != helpers[0] || !frame_is(frame, helpers[0]) || !PyLong_CheckExact(offset) ||
            PyLong_AsLong(offset) != PyFrame_GetLasti(frame)) goto failed;
    long position = PyLong_AsLong(offset);
    PyObject *bytes = PyCode_GetCode((PyCodeObject *)code);
    int terminal = bytes && position >= 0 && position < PyBytes_GET_SIZE(bytes) &&
        ((unsigned char)PyBytes_AS_STRING(bytes)[position] == RETURN_VALUE
#ifdef RETURN_CONST
         || (unsigned char)PyBytes_AS_STRING(bytes)[position] == RETURN_CONST
#endif
        );
    Py_XDECREF(bytes);
    if (!terminal) goto failed;
    Pending *slot = find(frame);
    int scope = in_scope(frame);
    if (scope < 0 || !slot) goto failed;
    if (!scope) { clear(slot); PyErr_SetRaisedException(saved); Py_RETURN_NONE; }
    PyObject *root = slot && slot->root ? referent(slot->root) : NULL;
    int valid = root && root == result && slot->compiled;
    Py_XDECREF(root);
    if (!valid) goto failed;
    PyObject *path = Py_NewRef(slot->path);
    uint64_t sequence = slot->sequence, loader = slot->loader;
    int optimize = slot->optimization;
    clear(slot);
    int status = otelc_py_origin_module(result, PyBytes_AS_STRING(path), sequence, loader, 0, optimize);
    Py_DECREF(path);
    if (status) goto failed;
    PyErr_SetRaisedException(saved);
    Py_RETURN_NONE;
failed:
    if (fail(saved, 0)) return NULL;
    Py_RETURN_NONE;
}

int otelc_py_origin_loader_ready(void) {
    if (active || owner) return -1;
    PyObject *value = otelc_py_origin_loader_type();
    if (!value) return -1;
    loader_type = Py_NewRef(value);
    for (unsigned index = 0; index < 4; ++index) {
        value = otelc_py_origin_helper_value(index);
        if (!value || !PyFunction_Check(value)) return -1;
        helpers[index] = Py_NewRef(PyFunction_GetCode(value));
    }
    value = otelc_py_origin_helper_value(4);
    if (!value || !PyCFunction_Check(value) || !PyVectorcall_Function(value)) return -1;
    compiler = Py_NewRef(value);
    original_compile = ((PyCFunctionObject *)compiler)->vectorcall;
    static PyMethodDef method = {"loader_return", returned, METH_VARARGS, NULL};
    PyObject *callback = PyCFunction_New(&method, NULL);
    int result = callback && !otelc_py_origin_local_callback(helpers[0], RETURN_EVENT, RETURN_EVENT | 1, callback);
    Py_XDECREF(callback);
    if (!result) return -1;
    owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    owner_pid = getpid();
    active = 1;
    ((PyCFunctionObject *)compiler)->vectorcall = compile_call;
    return 0;
}

int otelc_py_origin_loader_active(void) { return active; }
#else
int otelc_py_origin_loader_root(const char *path) { (void)path; return -1; }
int otelc_py_origin_loader_ready(void) { return -1; }
int otelc_py_origin_loader_active(void) { return 0; }
int otelc_py_origin_loader_start(PyObject *code) { (void)code; return 0; }
int otelc_py_origin_loader_read(PyObject *path, PyObject *bytes, uint64_t sequence) {
    (void)path; (void)bytes; (void)sequence;
    return 0;
}
void otelc_py_origin_loader_cancel(void) { }
#endif
