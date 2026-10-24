#ifndef OTELC_PYTHON_ORIGIN_PINS_H
#define OTELC_PYTHON_ORIGIN_PINS_H

#include <Python.h>

/* Install before interpreter initialisation. Readiness checks the owning
   interpreter and the original functions AND their original code objects.
   This guard alone does not establish a loader transaction or code origin. */
int otelc_py_origin_pins_install(void);
int otelc_py_origin_pins_ready(void);

enum otelc_py_monitor_method {
    OTELC_MONITOR_GET, OTELC_MONITOR_USE, OTELC_MONITOR_CALLBACK,
    OTELC_MONITOR_LOCAL, OTELC_MONITOR_FREE, OTELC_MONITOR_DISABLE, OTELC_MONITOR_METHODS
};
int otelc_py_origin_monitor_enable(void);
PyObject *otelc_py_origin_monitor_value(unsigned index);
PyObject *otelc_py_origin_helper_value(unsigned index);
PyObject *otelc_py_origin_loader_type(void);

#endif
