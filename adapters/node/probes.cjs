// Isolated bindings preserve applications that shadow globalThis, Symbol or process.
const key = Symbol.for('quux.otelc.runtime');
exports.enter = name => globalThis[key]?.enter(name) ?? 0;
exports.exit = (token, unwound) => globalThis[key]?.exit(token, unwound);
exports.enterAsync = (name, id) => globalThis[key]?.enterAsync?.(name, id);
exports.attach = (token, previous) => globalThis[key]?.attach?.(token, previous) ?? null;
exports.detach = previous => globalThis[key]?.detach?.(previous);
exports.suspend = (previous, value) => globalThis[key]?.suspend?.(previous, value) ?? value;
// A lifecycle observer failure must not replace application construction/throw.
const lifetime = action => {
  const runtime = globalThis[key];
  try { return action(runtime); } catch { runtime?.lifetimes?.lose('invalid'); }
};
exports.lifetimeCreation = () => lifetime(runtime => runtime?.parent());
exports.lifetimeType = (constructor, name) => lifetime(runtime => runtime?.lifetimes?.type(constructor, name));
exports.lifetimeAdmit = (value, constructor, name, creation) => lifetime(runtime => runtime?.lifetimes?.admit(value, constructor, name, creation));
exports.lifetimeFailed = (constructor, name) => lifetime(runtime => runtime?.lifetimes?.failedConstruction(constructor, name));
