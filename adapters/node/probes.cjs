// Isolated bindings preserve applications that shadow globalThis, Symbol or process.
const key = Symbol.for('quux.otelc.runtime');
exports.enter = name => globalThis[key]?.enter(name) ?? 0;
exports.exit = (token, unwound) => globalThis[key]?.exit(token, unwound);
exports.enterAsync = (name, id) => globalThis[key]?.enterAsync?.(name, id);
exports.attach = (token, previous) => globalThis[key]?.attach?.(token, previous) ?? null;
exports.detach = previous => globalThis[key]?.detach?.(previous);
exports.suspend = (previous, value) => globalThis[key]?.suspend?.(previous, value) ?? value;
