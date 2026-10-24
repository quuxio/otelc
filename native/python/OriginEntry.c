/* Observe an explicit, pinned native source-entry transaction without an SDK. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include <opcode.h>
#include "InputJournal.h"
#include "NativeEncoder.h"
#include "OriginEntry.h"
#include "OriginPins.h"
#include "OriginChain.h"
#include "OriginLoader.h"

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
    size_t root;
    int executed;
    int offset;
    uint64_t sdk;
} OriginCode;

static OriginCode codes[CODE_LIMIT];
static size_t code_count;
static char *root_paths[CODE_LIMIT];
static size_t root_count, root_path_bytes;
static PyObject *helper_codes[4];
static int helper_events[4];
static size_t helper_count;
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

static int catalogue(PyObject *code, const uint64_t *edges, size_t depth, size_t root) {
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
    slot->root = root;
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
        if (catalogue(child, next, depth + 1, root)) return -1;
    }
    return 0;
}

static void release_tool(void);

static int execution(size_t index, int offset) {
    OriginCode *slot = &codes[index];
    if (slot->executed) return 0;
    unsigned char edges[EDGE_LIMIT * 8];
    for (size_t edge = 0; edge < slot->depth; ++edge) {
        for (size_t byte = 0; byte < 8; ++byte) {
            edges[edge * 8 + byte] = (unsigned char)(slot->edges[edge] >> (56 - byte * 8));
        }
    }
    uint64_t fields[] = {index + 1, slot->root + 1, (uint64_t)offset, PY_VERSION_HEX, slot->depth, 0};
    if (otelc_py_journal_origin(6, root_paths[slot->root], edges, slot->depth * 8, fields, 6)) {
        lost = 1; /* The raw writer already counted this failure. */
        return -1;
    }
    slot->executed = 1;
    slot->offset = offset;
    return 0;
}

int otelc_py_origin_sdk_join(PyObject *code, uint64_t sdk, int offset) {
    /* Journal-1 code capture has no origin observer and keeps its old contract. */
    if (!installed) return 0;
    if (!ready || lost || !same_owner()) return -1;
    PyObject *saved = PyErr_GetRaisedException();
    if (!sdk || offset < 0 || !own_tool()) goto failed;
    for (size_t index = 0; index < code_count; ++index) {
        OriginCode *slot = &codes[index];
        PyObject *actual = referent(slot->weak);
        int same = actual == code;
        Py_XDECREF(actual);
        if (!same) continue;
        if (slot->executed && slot->offset != offset) goto failed;
        if (slot->sdk) {
            if (slot->sdk != sdk) goto failed;
            PyErr_SetRaisedException(saved);
            return 0;
        }
        /* SDK and native monitoring tools may run in either order. The caller
           has authenticated this same code/RESUME; publish once before joining. */
        if (execution(index, offset)) goto failed;
        uint64_t fields[] = {sdk, index + 1, (uint64_t)offset, 0, 0, 0};
        if (otelc_py_journal_origin(7, root_paths[slot->root], NULL, 0, fields, 6)) {
            lost = 1; /* The raw writer already counted this failure. */
            goto failed;
        }
        slot->sdk = sdk;
        PyErr_SetRaisedException(saved);
        return 0;
    }
failed:
    release_tool();
    (void)failure(saved);
    return -1;
}

static PyObject *started(PyObject *unused, PyObject *arguments) {
    (void)unused;
    if (!same_owner() || lost) Py_RETURN_NONE;
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
    int helper = otelc_py_origin_loader_start(code);
    if (helper < 0) goto failed;
    if (helper) { PyErr_SetRaisedException(saved); Py_RETURN_NONE; }
    for (size_t index = 0; index < code_count; ++index) {
        OriginCode *slot = &codes[index];
        PyObject *existing = referent(slot->weak);
        int same = existing == code;
        Py_XDECREF(existing);
        if (!same) continue;
        if (execution(index, (int)offset)) goto failed;
        PyErr_SetRaisedException(saved);
        /* This is always a local event. No global VM events are enabled. */
        return Py_NewRef(monitor[OTELC_MONITOR_DISABLE]);
    }
failed:
    if (failure(saved)) return NULL;
    Py_RETURN_NONE;
}

static void release_tool(void) {
    otelc_py_origin_loader_cancel();
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
    for (size_t index = 0; index < helper_count; ++index) {
        PyObject *value = PyObject_CallFunction(monitor[OTELC_MONITOR_LOCAL], "iOi", tool, helper_codes[index], 0);
        Py_XDECREF(value);
        PyErr_Clear();
        value = PyObject_CallFunction(monitor[OTELC_MONITOR_CALLBACK], "iiO", tool, helper_events[index], Py_None);
        Py_XDECREF(value);
        PyErr_Clear();
        Py_CLEAR(helper_codes[index]);
    }
    helper_count = 0;
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

static int publish_root(PyObject *code, const char *path, uint64_t sequence,
                        uint64_t loader, unsigned flags, int optimize, unsigned route) {
    PyObject *filename = route == 3 ? PyUnicode_EncodeFSDefault(((PyCodeObject *)code)->co_filename) : NULL;
    const char *executing_path = route == 3 ? (filename ? PyBytes_AS_STRING(filename) : NULL) : path;
    if (!executing_path) return -1;
    size_t length = strlen(executing_path);
    if (filename && ((size_t)PyBytes_GET_SIZE(filename) != length || length > OTELC_PY_PATH_LIMIT)) {
        Py_DECREF(filename);
        return -1;
    }
    if (root_count == CODE_LIMIT || length + 1 > OTELC_PY_JOURNAL_LIMIT - root_path_bytes) {
        Py_XDECREF(filename);
        return -1;
    }
    char *retained = strdup(executing_path);
    Py_XDECREF(filename);
    if (!retained) return -1;
    PyObject *payload = otelc_py_native_encode(code);
    if (!payload) { free(retained); return -1; }
    uint64_t fields[] = {root_count + 1, root_count + 1, loader, sequence, route,
                         PY_VERSION_HEX, 2, flags, (uint64_t)optimize};
    int result = otelc_py_journal_origin(5, route == 3 ? retained : path,
        (const unsigned char *)PyBytes_AS_STRING(payload), (size_t)PyBytes_GET_SIZE(payload), fields, 9);
    Py_DECREF(payload);
    if (result) {
        free(retained);
        lost = 1; /* The raw writer already counted this failure. */
        return -1;
    }
    size_t root = root_count++;
    root_paths[root] = retained;
    root_path_bytes += length + 1;
    uint64_t edges[EDGE_LIMIT] = {0};
    return catalogue(code, edges, 0, root);
}

int otelc_py_origin_cached_module(PyObject *code, const char *input_path, const char *source_path,
                                  uint64_t sequence, uint64_t loader) {
    if (!ready || lost || !same_owner()) return -1;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *encoded = PyCode_Check(code) ? PyUnicode_EncodeFSDefault(((PyCodeObject *)code)->co_filename) : NULL;
    int valid = encoded && sequence && loader && input_path && source_path &&
        PyUnicode_CompareWithASCIIString(((PyCodeObject *)code)->co_name, "<module>") == 0 &&
        (size_t)PyBytes_GET_SIZE(encoded) == strlen(source_path) &&
        !memcmp(PyBytes_AS_STRING(encoded), source_path, strlen(source_path)) &&
        !otelc_py_origin_pins_ready() && own_tool();
    Py_XDECREF(encoded);
    /* 3 denotes unknown compiler optimisation: these are deserialised bytes,
       not a compiler invocation whose flags could be inferred from a filename. */
    if (!valid || publish_root(code, input_path, sequence, loader, 0, 3, 3)) {
        release_tool();
        (void)failure(saved);
        return -1;
    }
    PyErr_SetRaisedException(saved);
    return 0;
}

int otelc_py_origin_module(PyObject *code, const char *path, uint64_t sequence,
                            uint64_t loader, unsigned flags, int optimize) {
    if (!ready || lost || !same_owner()) return -1;
    PyObject *saved = PyErr_GetRaisedException();
    PyObject *encoded = PyCode_Check(code) ? PyUnicode_EncodeFSDefault(((PyCodeObject *)code)->co_filename) : NULL;
    int valid = encoded && sequence && loader && flags <= INT32_MAX && optimize >= 0 && optimize <= 2 &&
        (size_t)PyBytes_GET_SIZE(encoded) == strlen(path) && !memcmp(PyBytes_AS_STRING(encoded), path, strlen(path)) &&
        !otelc_py_origin_pins_ready() && own_tool();
    Py_XDECREF(encoded);
    if (!valid || publish_root(code, path, sequence, loader, flags, optimize, 2)) {
        release_tool();
        (void)failure(saved);
        return -1;
    }
    PyErr_SetRaisedException(saved);
    return 0;
}

int otelc_py_origin_local_callback(PyObject *code, int event, int events, PyObject *callback) {
    if (!ready || lost || !same_owner() || !PyCode_Check(code) || !own_tool() || helper_count == 4) return -1;
    PyObject *previous = PyObject_CallFunction(monitor[OTELC_MONITOR_CALLBACK], "iiO", tool, event, callback);
    if (!previous) return -1;
    int empty = previous == Py_None;
    Py_DECREF(previous);
    if (!empty || !own_tool()) return -1;
    helper_codes[helper_count] = Py_NewRef(code);
    helper_events[helper_count++] = event;
    PyObject *result = PyObject_CallFunction(monitor[OTELC_MONITOR_LOCAL], "iOi", tool, code, events);
    if (!result) return -1;
    Py_DECREF(result);
    return own_tool() ? 0 : -1;
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
    /* The checked native file path initialises compiler input flags to zero;
       future bits merged by compilation belong to the returned descriptor. */
    if (publish_root(code, input_path, input_sequence, 0, 0, optimization, 1)) goto failed;
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
int otelc_py_origin_cached_module(PyObject *code, const char *input_path, const char *source_path,
                                  uint64_t sequence, uint64_t loader) {
    (void)code; (void)input_path; (void)source_path; (void)sequence; (void)loader;
    return -1;
}
int otelc_py_origin_entry_install(void) { return 0; }
int otelc_py_origin_entry_ready(int optimization) { (void)optimization; return -1; }
int otelc_py_origin_entry_active(void) { return 0; }
void otelc_py_origin_entry_cancel(void) { }
int otelc_py_origin_sdk_join(PyObject *code, uint64_t sdk, int offset) {
    (void)code; (void)sdk; (void)offset;
    return 0;
}
int otelc_py_origin_module(PyObject *code, const char *path, uint64_t sequence,
                            uint64_t loader, unsigned flags, int optimization) {
    (void)code; (void)path; (void)sequence; (void)loader; (void)flags; (void)optimization;
    return -1;
}
int otelc_py_origin_local_callback(PyObject *code, int event, int events, PyObject *callback) {
    (void)code; (void)event; (void)events; (void)callback;
    return -1;
}
void otelc_py_origin_entry_read(const char *path, const unsigned char *bytes,
                               size_t length, uint64_t sequence) {
    (void)path; (void)bytes; (void)length; (void)sequence;
}
#endif
