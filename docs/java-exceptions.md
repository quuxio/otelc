# Java exception safety

<!-- markdownlint-disable MD013 -->

Instrumenting a function must not execute application exception getters or formatters. Even a standard `RuntimeException` can have a custom cause whose message getter changes application state. Catching failures from those getters does not undo their side effects.

otelc keeps the official Java agent, instrumenter, SDK, metrics customiser and asynchronous completion support. Its small function probe module replaces the stock method advice. It disables automatic cause unwrapping and wraps only the method span's exception recording. An escaping error sets ERROR status and adds one `exception` event with `exception.type` obtained from the final JVM class identity operation. It does not call `getMessage`, `getLocalizedMessage`, `getCause`, `toString`, `getStackTrace` or `printStackTrace`. Messages and stack traces are deliberately unavailable; a wrapper is reported as the actual thrown wrapper type.

The exception passed to the application is unchanged. For `CompletionStage` results, the official completion observer is attached but its derived stage is not substituted for the application's original stage. The span and admitted direct metrics complete when that stage completes, including exceptional completion. Other asynchronous strategies retain the official support implementation; broad reactive-library and classloader qualification remains outstanding. This change does not claim automatic task-context propagation.

The real-agent regression compares plain and instrumented source-free applications through six exception shapes, including nested custom causes, getters which throw, and a wrapper subclass overriding `getCause`. It preserves original source/class/tooling identities, stderr, synchronous throwable identity and future identity. In the same instrumented PID it tests all four live metric states, deferred completion, decoded ERROR events and final zero-active/zero-drop SDK evidence. The control using the stock method implementation fails because an application getter changes state.
