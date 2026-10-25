/* Capture fault tests; these synthetic reads are not application provenance. */
#define _POSIX_C_SOURCE 200809L
#include "ImportCapture.c"

#include <fcntl.h>
#include <string.h>
#include <unistd.h>

#define CHECK(condition) do { if (!(condition)) { PyErr_Print(); return __LINE__; } } while (0)

static PyObject *no_memory(PyTypeObject *type, Py_ssize_t size) {
    (void)type;
    (void)size;
    return PyErr_NoMemory();
}

static PyObject *regular_input(PyObject *path) {
    PyObject *open = io_attribute("open");
    if (!open) return NULL;
    PyObject *file = PyObject_CallFunction(open, "Os", path, "rb");
    Py_DECREF(open);
    return file;
}

int main(int argc, char **argv) {
    CHECK(argc == 4);
    int fd = open(argv[2], O_WRONLY | O_CREAT | O_EXCL, 0600);
    CHECK(fd >= 0 && otelc_py_journal_open(fd) == 0);
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, argv[3]);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    CHECK(!PyStatus_Exception(status));
    PyObject *path = PyUnicode_DecodeFSDefault(argv[1]);
    CHECK(path != NULL);
    PyObject *globals = PyModule_GetDict(PyImport_AddModule("__main__"));
    CHECK(globals && PyDict_SetItemString(globals, "input_path", path) == 0);
    /* Existing application method keys must survive observer installation.
       A shared ml_meth mutation would change their hash after insertion. */
    CHECK(PyRun_SimpleString(
        "import _io\n"
        "early_file = _io.FileIO(input_path, 'r')\n"
        "early_read = early_file.read\n"
        "early_hash = hash(early_read)\n"
        "early_dict = {early_read: 'original'}\n"
        "early_set = {early_read}\n"
        "early_frozen = frozenset([early_read])\n") == 0);
    CHECK(otelc_py_imports_install() == 0);
    CHECK(otelc_py_imports_ready() == 0);
    CHECK(PyRun_SimpleString(
        "assert hash(early_read) == early_hash\n"
        "assert early_dict[early_read] == 'original'\n"
        "assert early_read in early_set and early_read in early_frozen\n"
        "assert early_read == early_file.read\n"
        "assert early_read() == b'abc'\n"
        "early_file.close()\n") == 0);
    PyThreadState *main_thread = PyThreadState_Get();
    PyThreadState *other = Py_NewInterpreter();
    CHECK(other != NULL);
    int foreign_result = PyRun_SimpleString(
        "import _io\n"
        "with _io.FileIO('/dev/null', 'r') as file:\n"
        " assert file.read() == b''\n"
        " assert file.read == _io.FileIO.read.__get__(file, _io.FileIO)\n");
    Py_EndInterpreter(other);
    PyThreadState_Swap(main_thread);
    CHECK(foreign_result == 0);

    PyObject *input = regular_input(path);
    PyObject *unencodable = PyUnicode_FromOrdinal(0xd800);
    CHECK(input && unencodable);
    CHECK(wrap_file(input, unencodable) == input && !PyErr_Occurred());
    Py_DECREF(unencodable);
    Py_DECREF(input);

    input = regular_input(path);
    CHECK(input != NULL);
    PyTypeObject *type = (PyTypeObject *)tracked_type;
    allocfunc original = type->tp_alloc;
    type->tp_alloc = no_memory;
    CHECK(wrap_file(input, path) == input && !PyErr_Occurred());
    type->tp_alloc = original;
    PyObject *content = PyObject_CallMethod(input, "read", NULL);
    CHECK(content && PyBytes_CheckExact(content) && PyBytes_GET_SIZE(content) == 3 &&
          memcmp(PyBytes_AS_STRING(content), "abc", 3) == 0);
    Py_DECREF(content);
    Py_DECREF(input);

    input = regular_input(path);
    CHECK(input != NULL);
    PyObject *wrapper = wrap_file(input, path);
    CHECK(wrapper != input);
    PyObject *closed = PyObject_CallMethod(input, "close", NULL);
    CHECK(closed != NULL);
    Py_DECREF(closed);
    CHECK(file_read((InputFile *)wrapper, NULL) == NULL && PyErr_ExceptionMatches(PyExc_ValueError));
    PyErr_Clear();
    CHECK(file_enter((InputFile *)wrapper, NULL) == NULL && PyErr_ExceptionMatches(PyExc_ValueError));
    PyErr_Clear();
    Py_DECREF(wrapper);

    input = regular_input(path);
    CHECK(input != NULL);
    PyObject *number = PyLong_FromLong(PyObject_AsFileDescriptor(input));
    CHECK(number && PyDict_SetItemString(globals, "descriptor", number) == 0);
    Py_DECREF(number);
    CHECK(PyRun_SimpleString(
        "class FailedRead:\n"
        " def fileno(self): return descriptor\n"
        " def read(self): raise OSError('original read')\n"
        " def __enter__(self): return self\n"
        " def __exit__(self, *args): raise RuntimeError('original close')\n") == 0);
    PyObject *fake = PyObject_CallNoArgs(PyDict_GetItemString(globals, "FailedRead"));
    CHECK(fake != NULL);
    wrapper = wrap_file(fake, path);
    CHECK(wrapper != fake && PyDict_SetItemString(globals, "wrapped", wrapper) == 0);
    CHECK(PyRun_SimpleString(
        "try:\n"
        " with wrapped as file: file.read()\n"
        "except RuntimeError as error:\n"
        " assert str(error) == 'original close'\n"
        " assert isinstance(error.__context__, OSError)\n"
        " assert str(error.__context__) == 'original read'\n"
        "else: raise AssertionError('read/close failure was suppressed')\n") == 0);
    Py_DECREF(wrapper);
    Py_DECREF(input);
    Py_DECREF(path);
    if (Py_FinalizeEx()) return 78;
    otelc_py_journal_finish(1, 0);
    return 0;
}
