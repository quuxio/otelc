/* Observe original cache unmarshalling without changing the builtin's method definition. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include "InputJournal.h"
#include "OriginChain.h"
#include "OriginPins.h"
#include "OriginLoader.h"
#include "OriginEntry.h"
#include "OriginCache.h"

#if OTELC_ORIGIN_CHAIN_AVAILABLE
#include <unistd.h>

static PyObject *marshaller, *helper;
static vectorcallfunc original_loads;
static PyInterpreterState *owner;
static pid_t owner_pid;
static int active;
static PyObject *loads_call(PyObject *, PyObject *const *, size_t, PyObject *);

static PyObject *call_event(PyObject *unused, PyObject *arguments) {
    (void)unused; (void)arguments;
    /* A local CALL event keeps this frozen helper on the vectorcall dispatch
       path. Warm specialised builtin calls otherwise bypass that pointer. */
    Py_RETURN_NONE;
}

int otelc_py_origin_cache_active(void) { return active; }

void otelc_py_origin_cache_cancel(void) {
    if (!active) return;
    active = 0;
    if (((PyCFunctionObject *)marshaller)->vectorcall == loads_call) {
        ((PyCFunctionObject *)marshaller)->vectorcall = original_loads;
    }
}

static void failed(void) {
    otelc_py_journal_capture_failed();
    otelc_py_origin_loader_cancel();
    otelc_py_origin_entry_cancel();
}

static PyObject *loads_call(PyObject *callable, PyObject *const *args, size_t count, PyObject *names) {
    if (!active || getpid() != owner_pid || PyThreadState_GetInterpreter(PyThreadState_Get()) != owner) {
        return original_loads(callable, args, count, names);
    }
    PyFrameObject *frame = PyEval_GetFrame();
    PyCodeObject *code = frame ? PyFrame_GetCode(frame) : NULL;
    int context = code && (PyObject *)code == helper;
    Py_XDECREF(code);
    if (!context) return original_loads(callable, args, count, names);
    PyObject *saved = PyErr_GetRaisedException();
    int matched = PyVectorcall_NARGS(count) == 1 && !names
        ? otelc_py_origin_loader_cache_input(args[0]) : -1;
    if (matched < 0 || (matched && otelc_py_origin_pins_ready())) failed();
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    PyObject *result = original_loads(callable, args, count, names);
    if (!active || !matched || matched < 0 || !result) return result;
    saved = PyErr_GetRaisedException();
    if (otelc_py_origin_loader_cache_return(args[0], result)) failed();
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    return result;
}

int otelc_py_origin_cache_ready(void) {
    if (active || owner || !otelc_py_origin_loader_active()) return -1;
    PyObject *value = otelc_py_origin_cache_value(0);
    PyObject *loads = otelc_py_origin_cache_value(1);
    if (!value || !PyFunction_Check(value) || !loads || !PyCFunction_Check(loads) || !PyVectorcall_Function(loads)) return -1;
    helper = Py_NewRef(PyFunction_GetCode(value));
    static PyMethodDef event = {"cache_call", call_event, METH_VARARGS, NULL};
    PyObject *callback = PyCFunction_New(&event, NULL);
    int monitored = callback && !otelc_py_origin_local_callback(helper, 16, 16, callback);
    Py_XDECREF(callback);
    if (!monitored) return -1;
    marshaller = Py_NewRef(loads);
    original_loads = ((PyCFunctionObject *)marshaller)->vectorcall;
    owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    owner_pid = getpid();
    active = 1;
    ((PyCFunctionObject *)marshaller)->vectorcall = loads_call;
    return 0;
}
#else
int otelc_py_origin_cache_active(void) { return 0; }
void otelc_py_origin_cache_cancel(void) { }
int otelc_py_origin_cache_ready(void) { return -1; }
#endif
