/* Tool-owned entry: retain CPython's argument, script and shutdown machinery. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include "InputJournal.h"
#include "NativeEncoder.h"
#include "OriginPins.h"
#include "OriginEntry.h"
#include "OriginLoader.h"
#include "OriginCache.h"

#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

static void process_exit(void) {
    /* Py_AtExit records successful finalization first. Early/native _exit
       paths must not invent interpreter finalization or a source receipt. */
    otelc_py_journal_finish(0, 0);
}

static void checked(PyStatus status, PyConfig *config) {
    if (PyStatus_Exception(status)) {
        PyConfig_Clear(config);
        Py_ExitStatusException(status);
    }
}

int main(int argc, char **argv) {
    int cache_origins = argc > 1 && (!strcmp(argv[1], "--origin-loader-caches") ||
                                     !strcmp(argv[1], "--origin-loader-caches-root"));
    int rooted = argc > 1 && (!strcmp(argv[1], "--origin-loader-root") ||
                              !strcmp(argv[1], "--origin-loader-caches-root"));
    if (rooted && argc < 3) return 64;
    const char *root = rooted ? argv[2] : NULL;
    int loader_origins = rooted || cache_origins || (argc > 1 && !strcmp(argv[1], "--origin-loader"));
    int entry_origins = loader_origins || (argc > 1 && !strcmp(argv[1], "--origin-entry"));
    int origins = entry_origins || (argc > 1 && !strcmp(argv[1], "--origin-observer"));
    int options = rooted ? 2 : (origins ? 1 : 0);
    argc -= options; argv += options;
    /* driver inherited-journal-fd selected-python original-argv0 [native args] */
    if (argc < 5 || strcmp(argv[2], OTELC_PY_SELECTED_EXECUTABLE)) return 64;
    if (root && otelc_py_origin_loader_root(root)) return 64;
    char *end;
    errno = 0;
    long descriptor = strtol(argv[1], &end, 10);
    if (errno || end == argv[1] || *end || descriptor < 0 || descriptor > INT_MAX ||
            otelc_py_journal_open_version((int)descriptor, cache_origins ? 3 : (entry_origins ? 2 : 1))) return 64;
    if (atexit(process_exit)) return 78;
    if (entry_origins && otelc_py_origin_monitor_enable()) return 78;
    if (cache_origins && otelc_py_origin_cache_enable()) return 78;
    if (origins && otelc_py_origin_pins_install()) return 78;
    if (entry_origins && otelc_py_origin_entry_install()) return 78;
    if (otelc_py_imports_install()) return 78;
    if (otelc_py_code_install()) return 78;
    int native_argc = argc - 3;
    char **native_argv = malloc((size_t)native_argc * sizeof(char *));
    if (!native_argv) return 78;
    /* Parsing can exit before PyConfig_Read returns. Its usage/error prefix
       must already carry the original interpreter argv0, including venvs. */
    native_argv[0] = argv[3];
    for (int index = 1; index < native_argc; ++index) native_argv[index] = argv[index + 3];
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    checked(PyConfig_SetBytesArgv(&config, native_argc, native_argv), &config);
    free(native_argv);
    checked(PyConfig_Read(&config), &config);
    if (config.orig_argv.length) {
        checked(PyConfig_SetBytesString(&config, &config.orig_argv.items[0], argv[3]), &config);
    }
    /* Use the selected frontend's pre-site identity, then let its normal
       startup hooks run once. Forcing the host's post-site alias breaks -S. */
    checked(PyConfig_SetBytesString(&config, &config.executable, OTELC_PY_INITIAL_EXECUTABLE), &config);
    checked(PyConfig_SetBytesString(&config, &config.base_executable, OTELC_PY_BASE_EXECUTABLE), &config);
    checked(Py_InitializeFromConfig(&config), &config);
    if (origins && otelc_py_origin_pins_ready()) otelc_py_journal_capture_failed();
    if (origins && otelc_py_encoder_ready()) {
        PyConfig_Clear(&config);
        PyErr_Clear();
        Py_FinalizeEx();
        return 78;
    }
    int entry_ready = entry_origins && config.run_filename && !otelc_py_origin_entry_ready(config.optimization_level);
    if (entry_origins && !entry_ready) {
        otelc_py_journal_capture_failed();
        PyErr_Clear();
    }
    if (loader_origins && entry_ready && otelc_py_origin_loader_ready()) {
        otelc_py_journal_capture_failed();
        otelc_py_origin_entry_cancel();
        PyErr_Clear();
    }
    if (cache_origins && entry_ready && otelc_py_origin_cache_ready()) {
        otelc_py_journal_capture_failed();
        otelc_py_origin_loader_cancel();
        otelc_py_origin_entry_cancel();
        PyErr_Clear();
    }
    if (otelc_py_imports_ready()) {
        otelc_py_journal_capture_failed();
        PyErr_Clear();
    }
    if (config.run_filename) {
        PyObject *name = PyUnicode_FromWideChar(config.run_filename, -1);
        PyObject *encoded = name ? PyUnicode_EncodeFSDefault(name) : NULL;
        int ready = encoded ? otelc_py_capture_enable(PyBytes_AsString(encoded)) : -1;
        Py_XDECREF(encoded);
        Py_XDECREF(name);
        if (ready) {
            PyConfig_Clear(&config);
            PyErr_Clear();
            Py_FinalizeEx();
            return 78;
        }
    }
    PyConfig_Clear(&config);
    if (Py_AtExit(otelc_py_capture_finish)) {
        Py_FinalizeEx();
        return 78;
    }
    return Py_RunMain();
}
