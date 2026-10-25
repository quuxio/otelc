/* Native counterexamples: different compile/exec paths cannot borrow an entry token. */
#include <Python.h>
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "OriginChain.h"
static int inside, done;
static unsigned compiled, public_events, private_events, file_runs, executions;

static int private_audit(PyObject *root) {
    /* The private emitter is local in 3.14. Its exact address comes from the
       same offline pinned symbol map; no application event selects it. */
    Dl_info python;
    if (!dladdr((const void *)Py_GetVersion, &python)) return -1;
    void *address = (char *)python.dli_fbase + origin_chain[1].start;
    typedef int (*Audit)(PyThreadState *, const char *, const char *, ...);
    Audit emit;
    _Static_assert(sizeof(emit) == sizeof(address), "unsupported native ABI");
    memcpy(&emit, &address, sizeof(emit));
    return emit(PyThreadState_Get(), "exec", "O", root);
}

static int same_name(PyObject *name, const char *path) {
    PyObject *expected = PyUnicode_DecodeFSDefault(path);
    int same = expected && PyUnicode_CheckExact(name) && !PyUnicode_Compare(name, expected);
    Py_XDECREF(expected);
    return same;
}

static char *source_bytes(const char *path) {
    if (!getenv("OTELC_RESEARCH_EQUAL")) {
        const char *text = "spoofed_native_compiler_root = 123\n";
        char *copy = malloc(strlen(text) + 1);
        if (copy) strcpy(copy, text);
        return copy;
    }
    FILE *input = fopen(path, "rb");
    if (!input || fseek(input, 0, SEEK_END)) { if (input) fclose(input); return NULL; }
    long size = ftell(input);
    if (size < 0 || size > 65536 || fseek(input, 0, SEEK_SET)) { fclose(input); return NULL; }
    char *bytes = malloc((size_t)size + 1);
    if (!bytes || fread(bytes, 1, (size_t)size, input) != (size_t)size) {
        free(bytes); fclose(input); return NULL;
    }
    bytes[size] = 0;
    fclose(input);
    return bytes;
}

static int spoof(const char *event, PyObject *args, void *unused) {
    (void)unused;
    const char *mode = getenv("OTELC_RESEARCH_SPOOF");
    const char *trigger = getenv("OTELC_RESEARCH_TRIGGER");
    const char *path = getenv("OTELC_RESEARCH_ENTRY");
    if (!trigger) trigger = "compile";
    if (inside || done || !mode || !path || strcmp(event, trigger) || PyEval_GetFrame()) return 0;
    PyObject *filename = NULL;
    if (PyTuple_CheckExact(args) && !strcmp(trigger, "compile") && PyTuple_GET_SIZE(args) == 2) {
        filename = PyTuple_GET_ITEM(args, 1);
    } else if (PyTuple_CheckExact(args) && !strcmp(trigger, "exec") && PyTuple_GET_SIZE(args) == 1) {
        PyObject *code = PyTuple_GET_ITEM(args, 0);
        if (PyCode_Check(code)) filename = ((PyCodeObject *)code)->co_filename;
    }
    if (!filename || !same_name(filename, path)) return 0;
    inside = 1;
    done = 1;
    char *source = source_bytes(path);
    PyObject *saved = PyErr_GetRaisedException();
    if (!source) goto finished;
    if (!strcmp(mode, "file")) {
        FILE *file = tmpfile();
        if (!file) goto finished;
        size_t size = strlen(source);
        if (fwrite(source, 1, size, file) != size || fseek(file, 0, SEEK_SET)) { fclose(file); goto finished; }
        PyObject *space = PyDict_New();
        PyCompilerFlags flags = _PyCompilerFlags_INIT;
        PyObject *value = space ? PyRun_FileExFlags(file, path, Py_file_input, space, space, 1, &flags) : NULL;
        if (value) ++file_runs;
        if (!space) fclose(file);
        Py_XDECREF(value);
        Py_XDECREF(space);
    } else {
        /* Equal bytes still have distinct code objects and compile transactions. */
        for (unsigned attempt = 0; attempt < 2; ++attempt) {
            PyObject *root = Py_CompileString(source, path, Py_file_input);
            if (!root) break;
            ++compiled;
            if (!strcmp(mode, "private")) {
                if (!private_audit(root)) ++private_events;
            } else if (!PySys_Audit("exec", "O", root)) ++public_events;
            if (!strcmp(mode, "execute")) {
                PyObject *space = PyDict_New();
                PyObject *value = space ? PyEval_EvalCode(root, space, space) : NULL;
                if (value) ++executions;
                Py_XDECREF(value);
                Py_XDECREF(space);
            }
            Py_DECREF(root);
        }
    }
finished:
    free(source);
    PyErr_Clear();
    PyErr_SetRaisedException(saved);
    inside = 0;
    return 0;
}

int research_spoof_install(void) { return PySys_AddAuditHook(spoof, NULL); }

void research_control_finish(void) {
    const char *path = getenv("OTELC_RESEARCH_CONTROL_REPORT");
    FILE *output = path ? fopen(path, "w") : NULL;
    if (!output) return;
    fprintf(output, "{\"triggered\":%s,\"compiled\":%u,\"public_events\":%u,\"private_events\":%u,\"file_runs\":%u,\"executions\":%u}\n",
        done ? "true" : "false", compiled, public_events, private_events, file_runs, executions);
    fclose(output);
}

__attribute__((constructor)) static void install_controls(void) {
    if (atexit(research_control_finish) || research_spoof_install()) _Exit(88);
}
