/* Exercise the native guard without introducing product testing controls. */
#include <Python.h>
#include "OriginPins.h"
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    const char *mode = argv[1];
    int late = !strcmp(mode, "late");
    if (!late && otelc_py_origin_pins_install()) return 65;
    if (!late && otelc_py_origin_pins_install() != -1) return 66;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.site_import = 0;
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, argv[2]);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) return 67;
    int result = 1;
    if (late) {
        result = otelc_py_origin_pins_install() == -1 && otelc_py_origin_pins_ready() == -1 ? 0 : 1;
    } else if (otelc_py_origin_pins_ready()) {
        result = 2;
    } else if (!strcmp(mode, "ordinary")) {
        PyErr_SetString(PyExc_ValueError, "preserve the application exception");
        PyObject *error = PyErr_GetRaisedException();
        PyErr_SetRaisedException(Py_NewRef(error));
        int ready = otelc_py_origin_pins_ready();
        PyObject *after = PyErr_GetRaisedException();
        result = !ready && after == error ? 0 : 1;
        Py_XDECREF(after);
        Py_DECREF(error);
    } else if (!strcmp(mode, "fork")) {
        pid_t child = fork();
        if (!child) _exit(otelc_py_origin_pins_ready() == -1 ? 0 : 1);
        int child_status;
        result = child > 0 && waitpid(child, &child_status, 0) == child &&
            WIFEXITED(child_status) && !WEXITSTATUS(child_status) && !otelc_py_origin_pins_ready() ? 0 : 1;
    } else if (!strcmp(mode, "interpreter")) {
        PyThreadState *main = PyThreadState_Get();
        PyThreadState *other = Py_NewInterpreter();
        if (other) {
            result = otelc_py_origin_pins_ready() == -1 ? 0 : 1;
            Py_EndInterpreter(other);
            PyThreadState_Swap(main);
            if (otelc_py_origin_pins_ready()) result = 1;
        }
    } else {
        const char *change = NULL;
        if (!strcmp(mode, "module")) change = "sys.modules['_frozen_importlib_external'] = None";
        if (!strcmp(mode, "class")) change = "external.SourceLoader = None";
        if (!strcmp(mode, "function")) change = "external.SourceLoader.source_to_code = None";
        if (!strcmp(mode, "compiler")) change = "builtins.compile = lambda *args, **kwargs: None";
        if (!strcmp(mode, "replace")) change = "external.SourceLoader.source_to_code = lambda *args, **kwargs: None";
        if (!strcmp(mode, "code0")) change = "external.SourceLoader.get_code.__code__ = (lambda *args, **kwargs: None).__code__";
        if (!strcmp(mode, "code1")) change = "external.SourceLoader.source_to_code.__code__ = (lambda *args, **kwargs: None).__code__";
        if (!strcmp(mode, "code2")) change = "external.FileLoader.get_data.__code__ = (lambda *args, **kwargs: None).__code__";
        if (!strcmp(mode, "code3")) change = "internal._call_with_frames_removed.__code__ = (lambda *args, **kwargs: None).__code__";
        if (change && !PyRun_SimpleString("import sys, builtins\nimport _frozen_importlib_external as external\nimport _frozen_importlib as internal\n") &&
                !PyRun_SimpleString(change)) result = otelc_py_origin_pins_ready() == -1 ? 0 : 1;
    }
    PyErr_Clear();
    if (Py_FinalizeEx() < 0) return 68;
    return result;
}
