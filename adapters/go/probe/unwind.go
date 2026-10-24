package probe

import "runtime"

const maxUnwindFrames = 128

// unwind inspects bounded runtime frame metadata without recovering the panic.
// Stop at this invocation: an outer panic must not taint a normal function called
// by an application's deferred handler. An absent boundary remains unknown.
func unwind(boundary string) string {
	var pcs [maxUnwindFrames]uintptr
	count := runtime.Callers(2, pcs[:])
	frames := runtime.CallersFrames(pcs[:count])
	kind := "normal"
	for {
		frame, more := frames.Next()
		if frame.Function == boundary && boundary != "" {
			return kind
		}
		if kind == "normal" {
			switch frame.Function {
			case "runtime.gopanic":
				kind = "panic"
			case "runtime.Goexit":
				kind = "goexit"
			}
		}
		if !more {
			return "unknown"
		}
	}
}
