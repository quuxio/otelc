#ifndef OTELC_PYTHON_ORIGIN_ENTRY_H
#define OTELC_PYTHON_ORIGIN_ENTRY_H

#include <stddef.h>
#include <stdint.h>

/* The driver owns one interpreter lifetime. Normal journal-1 launches never
   install this explicit, SDK-independent source-entry observer. */
int otelc_py_origin_entry_install(void);
int otelc_py_origin_entry_ready(int optimization);
int otelc_py_origin_entry_active(void);
void otelc_py_origin_entry_cancel(void);
void otelc_py_origin_entry_read(const char *path, const unsigned char *bytes,
                               size_t length, uint64_t sequence);

int otelc_py_origin_chain_ready(void);
int otelc_py_origin_chain_matches(void);

#endif
