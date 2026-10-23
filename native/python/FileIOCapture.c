/* Observe the original FileLoader's raw FileIO branch without replacing files. */
#define _POSIX_C_SOURCE 200809L
#include <Python.h>
#include "InputJournal.h"
#include "FileIOCapture.h"

#include <limits.h>
#include <string.h>
#include <unistd.h>

#define FILE_LIMIT 4096U

typedef struct { PyObject *weak, *path; uint64_t generation; int descriptor; struct stat opened; } FileSlot;
static FileSlot files[FILE_LIMIT];
static PyInterpreterState *owner;
static pid_t owner_pid;
static PyObject *file_type, *get_data_code, *read_method, *fileno_method;
static initproc original_init;
static getattrofunc original_getattro;
static _Thread_local PyFrameObject *suppressed_frame;
static uint64_t next_generation;

static int file_metadata(PyObject *file, struct stat *info, int *descriptor);

static int same_owner(void) {
    return owner && getpid() == owner_pid &&
        PyThreadState_GetInterpreter(PyThreadState_Get()) == owner;
}

static int owning_loader(void) {
    if (!same_owner()) return 0;
    PyFrameObject *frame = PyEval_GetFrame();
    if (!frame || frame == suppressed_frame) return 0;
    PyCodeObject *code = PyFrame_GetCode(frame);
    int matches = (PyObject *)code == get_data_code;
    Py_DECREF(code);
    return matches;
}

static PyObject *weak_target(PyObject *weak) {
#if PY_VERSION_HEX >= 0x030D0000
    PyObject *value = NULL;
    if (PyWeakref_GetRef(weak, &value) < 0) return NULL;
    return value;
#else
    PyObject *value = PyWeakref_GetObject(weak);
    return value && value != Py_None ? Py_NewRef(value) : NULL;
#endif
}

static FileSlot *slot(PyObject *file, int create) {
    FileSlot *available = NULL;
    for (size_t index = 0; index < FILE_LIMIT; ++index) {
        PyObject *value = files[index].weak ? weak_target(files[index].weak) : NULL;
        int matches = value == file;
        if (!value && !available) available = &files[index];
        Py_XDECREF(value);
        if (matches) return &files[index];
    }
    return create ? available : NULL;
}

static int remember(PyObject *file, PyObject *arguments) {
    FileSlot *entry = slot(file, 1);
    PyObject *path = PyTuple_GET_SIZE(arguments) ? PyTuple_GET_ITEM(arguments, 0) : NULL;
    PyObject *bytes = path && PyUnicode_CheckExact(path) ? PyUnicode_EncodeFSDefault(path) :
        path && PyBytes_CheckExact(path) ? Py_NewRef(path) : NULL;
    if (!entry || !bytes || !PyBytes_GET_SIZE(bytes) || PyBytes_GET_SIZE(bytes) > OTELC_PY_PATH_LIMIT ||
            memchr(PyBytes_AS_STRING(bytes), 0, (size_t)PyBytes_GET_SIZE(bytes))) {
        Py_XDECREF(bytes);
        return -1;
    }
    struct stat info;
    int descriptor;
    if (file_metadata(file, &info, &descriptor)) { Py_DECREF(bytes); return -1; }
    if (next_generation == UINT64_MAX) { Py_DECREF(bytes); return -1; }
    PyObject *weak = PyWeakref_NewRef(file, NULL);
    if (!weak) { Py_DECREF(bytes); return -1; }
    Py_XSETREF(entry->weak, weak);
    Py_XSETREF(entry->path, bytes);
    entry->descriptor = descriptor;
    entry->opened = info;
    entry->generation = ++next_generation;
    return 0;
}

static int file_init(PyObject *file, PyObject *arguments, PyObject *keywords) {
    FileSlot *prior = same_owner() ? slot(file, 0) : NULL;
    if (prior) {
        Py_CLEAR(prior->weak);
        Py_CLEAR(prior->path);
        prior->generation = 0;
        otelc_py_journal_capture_failed();
    }
    int loader = owning_loader();
    int capture = (PyObject *)Py_TYPE(file) == file_type && loader;
    int result = original_init(file, arguments, keywords);
    if (loader && !capture) otelc_py_journal_capture_failed();
    if (result < 0 || !capture) return result;
    if (remember(file, arguments)) {
        otelc_py_journal_capture_failed();
        if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt) || PyErr_ExceptionMatches(PyExc_SystemExit)) return -1;
        PyErr_Clear();
    }
    return result;
}

static PyObject *bound_method(PyObject *descriptor, PyObject *file) {
    return Py_TYPE(descriptor)->tp_descr_get(descriptor, file, (PyObject *)Py_TYPE(file));
}

static int file_metadata(PyObject *file, struct stat *info, int *actual_descriptor) {
    PyObject *method = bound_method(fileno_method, file);
    PyObject *number = method ? PyObject_CallNoArgs(method) : NULL;
    Py_XDECREF(method);
    if (!number) return -1;
    long descriptor = PyLong_AsLong(number);
    Py_DECREF(number);
    if (PyErr_Occurred() || descriptor < 0 || descriptor > INT_MAX ||
            fstat((int)descriptor, info) || !S_ISREG(info->st_mode)) return -1;
    *actual_descriptor = (int)descriptor;
    return 0;
}

static int same_snapshot(const struct stat *left, const struct stat *right) {
    if (left->st_dev != right->st_dev || left->st_ino != right->st_ino ||
            left->st_size != right->st_size || left->st_mode != right->st_mode) return 0;
#ifdef __APPLE__
    return left->st_mtimespec.tv_sec == right->st_mtimespec.tv_sec &&
        left->st_mtimespec.tv_nsec == right->st_mtimespec.tv_nsec &&
        left->st_ctimespec.tv_sec == right->st_ctimespec.tv_sec &&
        left->st_ctimespec.tv_nsec == right->st_ctimespec.tv_nsec;
#else
    return left->st_mtim.tv_sec == right->st_mtim.tv_sec && left->st_mtim.tv_nsec == right->st_mtim.tv_nsec &&
        left->st_ctim.tv_sec == right->st_ctim.tv_sec && left->st_ctim.tv_nsec == right->st_ctim.tv_nsec;
#endif
}

static PyObject *file_read(PyObject *file, PyTypeObject *defining_class,
                           PyObject *const *arguments, Py_ssize_t count, PyObject *keywords) {
    (void)defining_class;
    FileSlot *entry = owning_loader() ? slot(file, 0) : NULL;
    PyObject *path = entry ? Py_NewRef(entry->path) : NULL;
    uint64_t generation = entry ? entry->generation : 0;
    struct stat info;
    int descriptor;
    int capture = path && !file_metadata(file, &info, &descriptor) && descriptor == entry->descriptor &&
        info.st_dev == entry->opened.st_dev && info.st_ino == entry->opened.st_ino;
    if (path && !capture) {
        otelc_py_journal_capture_failed();
        if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt) || PyErr_ExceptionMatches(PyExc_SystemExit)) {
            Py_DECREF(path);
            return NULL;
        }
        PyErr_Clear();
    }
    /* Call the original bound C method, with the original literal arguments.
       No Python lookup or application formatting occurs in the writer lock. */
    PyObject *method = bound_method(read_method, file);
    PyObject *payload = method ? PyObject_Vectorcall(method, arguments, (size_t)count, keywords) : NULL;
    Py_XDECREF(method);
    if (capture && payload) {
        struct stat after;
        int current_descriptor;
        int stable = same_owner() && entry->generation == generation &&
            !file_metadata(file, &after, &current_descriptor) && current_descriptor == descriptor &&
            same_snapshot(&info, &after);
        if (!stable) {
            capture = 0;
            otelc_py_journal_capture_failed();
            if (PyErr_ExceptionMatches(PyExc_KeyboardInterrupt) || PyErr_ExceptionMatches(PyExc_SystemExit)) {
                Py_DECREF(path); Py_DECREF(payload);
                return NULL;
            }
            PyErr_Clear();
        }
    }
    if (capture) {
        if (payload && PyBytes_CheckExact(payload)) {
            (void)otelc_py_journal_import(PyBytes_AS_STRING(path),
                (const unsigned char *)PyBytes_AS_STRING(payload), (size_t)PyBytes_GET_SIZE(payload), &info);
        } else otelc_py_journal_capture_failed();
    }
    Py_XDECREF(path);
    return payload;
}

static PyMethodDef read_definition = {"read", _PyCFunction_CAST(file_read),
    METH_METHOD | METH_FASTCALL | METH_KEYWORDS, NULL};

static PyObject *file_getattro(PyObject *file, PyObject *name) {
    PyObject *result = original_getattro(file, name);
    if (!result || (PyObject *)Py_TYPE(file) != file_type || !PyUnicode_CheckExact(name) ||
            PyUnicode_CompareWithASCIIString(name, "read") || !owning_loader()) return result;
    if (!slot(file, 0)) {
        otelc_py_journal_capture_failed();
        return result;
    }
    /* Keep application replacements and their errors. Only the original C
       read method on an actually tracked private loader file is observed. */
    if (!PyCFunction_Check(result) || PyCFunction_GetSelf(result) != file ||
            PyCFunction_GetFunction(result) != ((PyMethodDescrObject *)read_method)->d_method->ml_meth) return result;
    PyObject *observed = PyCMethod_New(&read_definition, file, NULL, (PyTypeObject *)file_type);
    if (!observed) {
        otelc_py_journal_capture_failed();
        PyErr_Clear();
        return result;
    }
    Py_DECREF(result);
    return observed;
}

static PyObject *named_method(PyObject *dictionary, const char *name) {
    if (!PyDict_CheckExact(dictionary)) return NULL;
    Py_ssize_t position = 0;
    PyObject *key, *value;
    while (PyDict_Next(dictionary, &position, &key, &value)) {
        if (PyUnicode_CheckExact(key) && !PyUnicode_CompareWithASCIIString(key, name)) return value;
    }
    return NULL;
}

int otelc_py_fileio_install(PyObject *type, PyObject *loader_code) {
    if (owner || !type || !PyType_Check(type) || !PyCode_Check(loader_code)) return -1;
    PyTypeObject *file = (PyTypeObject *)type;
    if (strcmp(file->tp_name, "_io.FileIO") || !file->tp_init || !file->tp_getattro) return -1;
    PyObject *read = named_method(file->tp_dict, "read");
    PyObject *fileno = named_method(file->tp_dict, "fileno");
    PyObject *initialiser = named_method(file->tp_dict, "__init__");
    if (!read || !fileno || !initialiser || !Py_IS_TYPE(read, &PyMethodDescr_Type) ||
            !Py_IS_TYPE(fileno, &PyMethodDescr_Type) || !Py_IS_TYPE(initialiser, &PyWrapperDescr_Type) ||
            ((PyMethodDescrObject *)read)->d_method->ml_flags != read_definition.ml_flags) return -1;
    initproc described;
    _Static_assert(sizeof(described) == sizeof(((PyWrapperDescrObject *)initialiser)->d_wrapped),
                   "unsupported CPython slot-wrapper ABI");
    memcpy(&described, &((PyWrapperDescrObject *)initialiser)->d_wrapped, sizeof(described));
    if (described != file->tp_init) return -1;
    file_type = Py_NewRef(type);
    get_data_code = Py_NewRef(loader_code);
    read_method = Py_NewRef(read);
    fileno_method = Py_NewRef(fileno);
    original_init = file->tp_init;
    original_getattro = file->tp_getattro;
    owner = PyThreadState_GetInterpreter(PyThreadState_Get());
    owner_pid = getpid();
    file->tp_init = file_init;
    file->tp_getattro = file_getattro;
    /* Explicit FileIO.__init__ and inherited subclass initialisers use this
       same descriptor. Keep its identity while observing reinitialisation. */
    initproc observed_init = file_init;
    memcpy(&((PyWrapperDescrObject *)initialiser)->d_wrapped, &observed_init, sizeof(observed_init));
    read_definition.ml_doc = ((PyMethodDescrObject *)read)->d_method->ml_doc;
    PyType_Modified(file);
    return 0;
}

PyFrameObject *otelc_py_fileio_suppress(PyFrameObject *frame) {
    /* The caller's C stack owns the previous scope; no Python frame is kept
       beyond its real open_code call. A nested loader has a different frame. */
    PyFrameObject *previous = suppressed_frame;
    suppressed_frame = frame;
    return previous;
}
