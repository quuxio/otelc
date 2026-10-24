/* Observe an explicit, pinned native source-entry transaction without an SDK. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include <opcode.h>
#include "InputJournal.h"
#include "NativeEncoder.h"
#include "OriginEntry.h"
#include "OriginPins.h"
#include "OriginChain.h"

#include <string.h>
#include <unistd.h>

#if OTELC_ORIGIN_CHAIN_AVAILABLE
#include <pthread.h>
#include <stdlib.h>

/* Exact pinned ABI; this declaration moved to an internal header in 3.14. */
extern const PyConfig *_Py_GetConfig(void);
#define CODE_LIMIT 4096U
#define EDGE_LIMIT 128U
typedef struct {
    PyObject *weak;
    uint64_t edges[EDGE_LIMIT];
    size_t depth;
    int executed;
} OriginCode;

static OriginCode codes[CODE_LIMIT];
static size_t code_count;
static uint64_t input_sequence;
static char *input_path;
static PyInterpreterState *owner;
static pid_t owner_pid;
static pthread_t entry_thread;
static PyObject *monitor[OTELC_MONITOR_METHODS];
static PyObject *tool_name;
static int installed, ready, consumed, lost, optimization, tool = -1;
static _Thread_local int busy;

static int same_owner(void) {
    return installed && getpid() == owner_pid &&
        PyThreadState_GetInterpreter(PyThreadState_Get()) == owner;
}

int otelc_py_origin_entry_active(void) {
    return ready && !lost && getpid() == owner_pid && pthread_equal(entry_thread, pthread_self());
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

static int failure(PyObject *saved) {
    if (!lost) otelc_py_journal_capture_failed();
    lost = 1;
    /* A capture error cannot swallow an actual application interrupt or exit. */
    if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt) || PyErr_ExceptionMatches(PyExc_SystemExit)) {
        Py_XDECREF(saved);
        return -1;
    }
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    return 0;
}

static int own_tool(void) {
    if (tool < 0 || !tool_name) return 0;
    PyObject *current = PyObject_CallFunction(monitor[OTELC_MONITOR_GET], "i", tool);
    int same = current == tool_name;
    Py_XDECREF(current);
    return same;
}

static int catalogue(PyObject *code, const uint64_t *edges, size_t depth) {
    for (size_t index = 0; index < code_count; ++index) {
        PyObject *existing = referent(codes[index].weak);
        int same = existing == code;
        Py_XDECREF(existing);
        if (same) return 0;
    }
    if (code_count == CODE_LIMIT || depth > EDGE_LIMIT || !PyCode_Check(code)) return -1;
    PyObject *weak = PyWeakref_NewRef(code, NULL);
    if (!weak) return -1;
    OriginCode *slot = &codes[code_count++];
    slot->weak = weak;
    slot->depth = depth;
    memcpy(slot->edges, edges, depth * sizeof(*edges));
    if (!own_tool()) return -1;
    PyObject *result = PyObject_CallFunction(monitor[OTELC_MONITOR_LOCAL], "iOi", tool, code, 1);
    if (!result) return -1;
    Py_DECREF(result);
    if (!own_tool()) return -1;
    PyObject *constants = ((PyCodeObject *)code)->co_consts;
    for (Py_ssize_t index = 0; index < PyTuple_GET_SIZE(constants); ++index) {
        PyObject *child = PyTuple_GET_ITEM(constants, index);
        if (!PyCode_Check(child)) continue;
        if (depth == EDGE_LIMIT) return -1;
        uint64_t next[EDGE_LIMIT];
        memcpy(next, edges, depth * sizeof(*edges));
        next[depth] = (uint64_t)index;
        if (catalogue(child, next, depth + 1)) return -1;
    }
    return 0;
}

static PyObject *started(PyObject *unused, PyObject *arguments) {
    (void)unused;
    if (!same_owner() || lost || !consumed) Py_RETURN_NONE;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *code, *position;
    if (!PyArg_UnpackTuple(arguments, "origin_start", 2, 2, &code, &position) ||
            !PyCode_Check(code) || !PyLong_CheckExact(position)) goto failed;
    PyFrameObject *frame = PyEval_GetFrame();
    PyCodeObject *actual = frame ? PyFrame_GetCode(frame) : NULL;
    long offset = PyLong_AsLong(position);
    int valid = actual && (PyObject *)actual == code && offset >= 0 &&
        PyFrame_GetLasti(frame) == offset;
    Py_XDECREF(actual);
    if (!valid || PyErr_Occurred()) goto failed;
    PyObject *bytes = PyCode_GetCode((PyCodeObject *)code);
    if (!bytes) goto failed;
    valid = offset < PyBytes_GET_SIZE(bytes) && (unsigned char)PyBytes_AS_STRING(bytes)[offset] == RESUME;
    Py_DECREF(bytes);
    if (!valid) goto failed;
    for (size_t index = 0; index < code_count; ++index) {
        OriginCode *slot = &codes[index];
        PyObject *existing = referent(slot->weak);
        int same = existing == code;
        Py_XDECREF(existing);
        if (!same) continue;
        if (!slot->executed) {
            unsigned char edges[EDGE_LIMIT * 8];
            for (size_t edge = 0; edge < slot->depth; ++edge) {
                for (size_t byte = 0; byte < 8; ++byte) {
                    edges[edge * 8 + byte] = (unsigned char)(slot->edges[edge] >> (56 - byte * 8));
                }
            }
            slot->executed = 1;
            uint64_t fields[] = {index + 1, 1, (uint64_t)offset, PY_VERSION_HEX, slot->depth, 0};
            if (otelc_py_journal_origin(6, input_path, edges, slot->depth * 8, fields, 6)) {
                lost = 1; /* The raw writer already counted this failure. */
                goto failed;
            }
        }
        PyErr_SetRaisedException(saved);
        /* This is always a local event. No global VM events are enabled. */
        return Py_NewRef(monitor[OTELC_MONITOR_DISABLE]);
    }
failed:
    if (failure(saved)) return NULL;
    Py_RETURN_NONE;
}

static void release_tool(void) {
    if (tool < 0) return;
    PyObject *saved = PyErr_GetRaisedException();
    if (!own_tool()) {
        tool = -1;
        PyErr_Clear();
        PyErr_SetRaisedException(saved);
        return;
    }
    for (size_t index = 0; index < code_count; ++index) {
        PyObject *code = referent(codes[index].weak);
        if (code) {
            PyObject *value = PyObject_CallFunction(monitor[OTELC_MONITOR_LOCAL], "iOi", tool, code, 0);
            Py_XDECREF(value);
            Py_DECREF(code);
            PyErr_Clear();
        }
    }
    /* 3.12 free_tool_id does not unregister callbacks or local events. */
    PyObject *value = PyObject_CallFunction(monitor[OTELC_MONITOR_CALLBACK], "iiO", tool, 1, Py_None);
    Py_XDECREF(value);
    PyErr_Clear();
    value = PyObject_CallFunction(monitor[OTELC_MONITOR_FREE], "i", tool);
    Py_XDECREF(value);
    PyErr_Clear();
    tool = -1;
    PyErr_SetRaisedException(saved);
}

void otelc_py_origin_entry_cancel(void) {
    if (!ready || !same_owner()) return;
    /* The source writer already records this capture failure. */
    lost = 1;
    release_tool();
}

int otelc_py_origin_entry_ready(int optimize) {
    if (!installed || ready || getpid() != owner_pid) return -1;
    owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    if (optimize < 0 || optimize > 2 || _Py_GetConfig()->optimization_level != optimize ||
            otelc_py_origin_pins_ready() || otelc_py_origin_chain_ready() || otelc_py_encoder_ready()) return -1;
    optimization = optimize;
    for (unsigned index = 0; index < OTELC_MONITOR_METHODS; ++index) {
        PyObject *value = otelc_py_origin_monitor_value(index);
        if (!value) return -1;
        monitor[index] = Py_NewRef(value);
    }
    const int choices[] = {2, 5, 3, 4};
    for (size_t index = 0; index < sizeof(choices) / sizeof(choices[0]); ++index) {
        PyObject *value = PyObject_CallFunction(monitor[OTELC_MONITOR_GET], "i", choices[index]);
        if (!value) goto failed;
        int available = value == Py_None;
        Py_DECREF(value);
        if (!available) continue;
        PyObject *name = PyUnicode_FromString("quux.otelc.origins");
        value = name ? PyObject_CallFunction(monitor[OTELC_MONITOR_USE], "iO", choices[index], name) : NULL;
        if (!value) {
            Py_XDECREF(name);
            goto failed;
        }
        Py_DECREF(value);
        tool = choices[index];
        tool_name = name;
        if (!own_tool()) goto failed;
        break;
    }
    if (tool < 0) return -1;
    static PyMethodDef method = {"origin_start", started, METH_VARARGS, NULL};
    PyObject *callback = PyCFunction_New(&method, NULL);
    PyObject *previous = callback ? PyObject_CallFunction(monitor[OTELC_MONITOR_CALLBACK], "iiO", tool, 1, callback) : NULL;
    Py_XDECREF(callback);
    if (!previous) goto failed;
    int empty = previous == Py_None;
    Py_DECREF(previous);
    if (!empty || !own_tool()) goto failed;
    ready = 1;
    return 0;
failed:
    release_tool();
    return -1;
}

void otelc_py_origin_entry_read(const char *path, const unsigned char *bytes,
                               size_t length, uint64_t sequence) {
    if (!ready || lost || !same_owner() || !pthread_equal(entry_thread, pthread_self())) return;
    PyObject *saved = PyErr_GetRaisedException();
    unsigned long magic = OTELC_ORIGIN_MAGIC_PREFIX;
    size_t size = strlen(path);
    if (!sequence) {
        lost = 1; /* The source writer already counted the failed read. */
        goto failed;
    }
    if (input_sequence || (size >= 4 && !strcmp(path + size - 4, ".pyc")) ||
            (length >= 2 && bytes[0] == (magic & 255) && bytes[1] == ((magic >> 8) & 255))) goto failed;
    input_path = strdup(path);
    if (!input_path) goto failed;
    input_sequence = sequence;
    PyErr_SetRaisedException(saved);
    return;
failed:
    release_tool();
    (void)failure(saved);
}

static int audit(const char *event, PyObject *arguments, void *unused) {
    (void)unused;
    if (strcmp(event, "exec") || !ready || lost || consumed || busy || !input_sequence ||
            !same_owner() || !pthread_equal(entry_thread, pthread_self()) || PyEval_GetFrame()) return 0;
    if (!otelc_py_origin_chain_matches()) return 0;
    PyObject *saved = PyErr_GetRaisedException();
    busy = 1;
    PyObject *code = PyTuple_CheckExact(arguments) && PyTuple_GET_SIZE(arguments) == 1
        ? PyTuple_GET_ITEM(arguments, 0) : NULL;
    PyObject *path = code && PyCode_Check(code) ? PyUnicode_EncodeFSDefault(((PyCodeObject *)code)->co_filename) : NULL;
    if (!path || (size_t)PyBytes_GET_SIZE(path) != strlen(input_path) ||
            memcmp(PyBytes_AS_STRING(path), input_path, strlen(input_path)) ||
            _Py_GetConfig()->optimization_level != optimization || otelc_py_origin_pins_ready() || !own_tool()) goto failed;
    PyObject *payload = otelc_py_native_encode(code);
    if (!payload) goto failed;
    /* The checked native file path initialises compiler input flags to zero;
       future bits merged by compilation belong to the returned descriptor. */
    uint64_t fields[] = {1, 1, 0, input_sequence, 1, PY_VERSION_HEX, 2, 0, (uint64_t)optimization};
    int result = otelc_py_journal_origin(5, input_path,
        (const unsigned char *)PyBytes_AS_STRING(payload), (size_t)PyBytes_GET_SIZE(payload), fields, 9);
    Py_DECREF(payload);
    if (result) {
        lost = 1; /* The raw writer already counted this failure. */
        goto failed;
    }
    uint64_t edges[EDGE_LIMIT] = {0};
    if (catalogue(code, edges, 0)) goto failed;
    consumed = 1;
    busy = 0;
    Py_DECREF(path);
    PyErr_SetRaisedException(saved);
    return 0;
failed:
    busy = 0;
    Py_XDECREF(path);
    release_tool();
    return failure(saved);
}

int otelc_py_origin_entry_install(void) {
    if (installed || Py_IsInitialized()) return -1;
    owner_pid = getpid();
    entry_thread = pthread_self();
    if (PySys_AddAuditHook(audit, NULL)) return -1;
    installed = 1;
    return 0;
}
#else
int otelc_py_origin_entry_install(void) { return 0; }
int otelc_py_origin_entry_ready(int optimization) { (void)optimization; return -1; }
int otelc_py_origin_entry_active(void) { return 0; }
void otelc_py_origin_entry_cancel(void) { }
void otelc_py_origin_entry_read(const char *path, const unsigned char *bytes,
                               size_t length, uint64_t sequence) {
    (void)path; (void)bytes; (void)length; (void)sequence;
}
#endif
