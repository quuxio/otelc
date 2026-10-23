/* Retain ordinary-loader read results; explicit open_code keeps its real file. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include "InputJournal.h"
#include "FileIOCapture.h"

/* One native entry owns one interpreter lifetime. Cached objects are frozen or
   builtin toolchain objects, never application values. Other interpreters pass
   through with capture loss; they never access these Python references. */
static PyInterpreterState *owner;
static PyObject *get_data_code, *buffered_reader, *builtin_open, *tracked_type;

typedef struct {
    PyObject_HEAD
    PyObject *file;
    PyObject *path;
} InputFile;

static void file_dealloc(InputFile *self) {
    PyTypeObject *type = Py_TYPE(self);
    Py_XDECREF(self->file);
    Py_XDECREF(self->path);
    type->tp_free((PyObject *)self);
    Py_DECREF(type);
}

static PyObject *file_enter(InputFile *self, PyObject *unused) {
    (void)unused;
    PyObject *entered = PyObject_CallMethod(self->file, "__enter__", NULL);
    if (!entered) return NULL;
    Py_DECREF(entered);
    return Py_NewRef((PyObject *)self);
}

static PyObject *file_exit(InputFile *self, PyObject *arguments) {
    PyObject *exit = PyObject_GetAttrString(self->file, "__exit__");
    if (!exit) return NULL;
    PyObject *result = PyObject_CallObject(exit, arguments);
    Py_DECREF(exit);
    return result;
}

static PyObject *file_read(InputFile *self, PyObject *unused) {
    (void)unused;
    struct stat info;
    int descriptor = PyObject_AsFileDescriptor(self->file);
    int valid = descriptor >= 0 && !fstat(descriptor, &info) && S_ISREG(info.st_mode);
    if (!valid) {
        PyErr_Clear();
        otelc_py_journal_capture_failed();
    }
    /* Keep the original unbounded read at the loader's read call site. Its
       immutable bytes go directly to the loader; no snapshot copy or seek is
       needed. Capture limits constrain the journal, never the application IO. */
    PyObject *payload = PyObject_CallMethod(self->file, "read", NULL);
    if (!payload) {
        if (valid) otelc_py_journal_capture_failed();
        return NULL;
    }
    if (valid) {
        if (PyBytes_CheckExact(payload)) {
            (void)otelc_py_journal_import(PyBytes_AS_STRING(self->path),
                (const unsigned char *)PyBytes_AS_STRING(payload), (size_t)PyBytes_GET_SIZE(payload), &info);
        } else {
            otelc_py_journal_capture_failed();
        }
    }
    return payload;
}

static PyMethodDef file_methods[] = {
    {"__enter__", (PyCFunction)file_enter, METH_NOARGS, NULL},
    {"__exit__", (PyCFunction)file_exit, METH_VARARGS, NULL},
    {"read", (PyCFunction)file_read, METH_NOARGS, NULL},
    {NULL, NULL, 0, NULL}
};

static PyType_Slot file_slots[] = {
    {Py_tp_dealloc, (void *)file_dealloc},
    {Py_tp_methods, file_methods},
    {0, NULL}
};

static PyType_Spec file_spec = {
    "otelc.native.InputFile", sizeof(InputFile), 0,
    Py_TPFLAGS_DEFAULT | Py_TPFLAGS_DISALLOW_INSTANTIATION | Py_TPFLAGS_IMMUTABLETYPE, file_slots
};

static PyObject *io_attribute(const char *name) {
    PyObject *module = PyDict_GetItemString(PyImport_GetModuleDict(), "_io");
    if (!module) {
        PyErr_SetString(PyExc_RuntimeError, "native input capture requires initialized _io");
        return NULL;
    }
    return PyObject_GetAttrString(module, name);
}

int otelc_py_imports_ready(void) {
    PyInterpreterState *current = PyThreadState_GetInterpreter(PyThreadState_Get());
    if (owner) return current == owner ? 0 : -1;
    PyObject *module = PyDict_GetItemString(PyImport_GetModuleDict(), "_frozen_importlib_external");
    PyObject *loader = module && PyModule_Check(module)
        ? PyDict_GetItemString(PyModule_GetDict(module), "FileLoader") : NULL;
    PyObject *function = loader && PyType_Check(loader)
        ? PyDict_GetItemString(((PyTypeObject *)loader)->tp_dict, "get_data") : NULL;
    if (!function || !PyFunction_Check(function)) return -1;
    PyObject *reader = io_attribute("BufferedReader");
    PyObject *open = reader ? io_attribute("open") : NULL;
    PyObject *type = open ? PyType_FromSpec(&file_spec) : NULL;
    if (!reader || !open || !type || !PyType_Check(reader) || !PyCFunction_Check(open)) {
        Py_XDECREF(reader);
        Py_XDECREF(open);
        Py_XDECREF(type);
        return -1;
    }
    PyObject *raw = io_attribute("FileIO");
    int raw_ready = raw ? otelc_py_fileio_install(raw, PyFunction_GetCode(function)) : -1;
    Py_XDECREF(raw);
    if (raw_ready) {
        Py_DECREF(reader); Py_DECREF(open); Py_DECREF(type);
        return -1;
    }
    get_data_code = Py_NewRef(PyFunction_GetCode(function));
    buffered_reader = reader;
    builtin_open = open;
    tracked_type = type;
    owner = current;
    return 0;
}

static int ordinary_loader(void) {
    if (otelc_py_imports_ready()) {
        PyErr_Clear();
        otelc_py_journal_capture_failed();
        return 0;
    }
    PyFrameObject *frame = PyEval_GetFrame();
    if (!frame) return 0;
    PyCodeObject *code = PyFrame_GetCode(frame);
    int matches = (PyObject *)code == get_data_code;
    Py_XDECREF(code);
    return matches;
}

static PyObject *wrap_file(PyObject *input, PyObject *path) {
    PyObject *encoded = PyUnicode_EncodeFSDefault(path);
    if (!encoded) {
        PyErr_Clear();
        otelc_py_journal_capture_failed();
        return input;
    }
    PyTypeObject *type = (PyTypeObject *)tracked_type;
    InputFile *wrapper = (InputFile *)type->tp_alloc(type, 0);
    if (!wrapper) {
        Py_DECREF(encoded);
        PyErr_Clear();
        otelc_py_journal_capture_failed();
        return input;
    }
    wrapper->file = input;
    wrapper->path = encoded;
    return (PyObject *)wrapper;
}

static PyObject *open_code(PyObject *path, void *unused) {
    (void)unused;
    int capture = ordinary_loader();
    /* Preserve CPython's dynamic _io.open lookup, including user overrides.
       Never invoke extra file methods on an application-owned wrapper. */
    PyObject *open = io_attribute("open");
    if (!open) return NULL;
    int ordinary_open = open == builtin_open;
    PyFrameObject *previous = otelc_py_fileio_suppress(PyEval_GetFrame());
    PyObject *input = PyObject_CallFunction(open, "Os", path, "rb");
    (void)otelc_py_fileio_suppress(previous);
    Py_DECREF(open);
    if (!input || !capture) return input;
    if (!ordinary_open || (PyObject *)Py_TYPE(input) != buffered_reader) {
        otelc_py_journal_capture_failed();
        return input;
    }
    return wrap_file(input, path);
}

int otelc_py_imports_install(void) {
    return PyFile_SetOpenCodeHook(open_code, NULL);
}
