// Isolated bindings preserve applications that shadow globalThis, Symbol or process.
const key = Symbol.for('quux.otelc.runtime');
exports.enter = name => globalThis[key]?.enter(name) ?? 0;
exports.exit = (token, unwound) => globalThis[key]?.exit(token, unwound);
