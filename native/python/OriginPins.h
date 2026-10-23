#ifndef OTELC_PYTHON_ORIGIN_PINS_H
#define OTELC_PYTHON_ORIGIN_PINS_H

/* Install before interpreter initialisation. Readiness checks the owning
   interpreter and the original functions AND their original code objects.
   This guard alone does not establish a loader transaction or code origin. */
int otelc_py_origin_pins_install(void);
int otelc_py_origin_pins_ready(void);

#endif
