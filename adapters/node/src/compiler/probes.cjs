// Private lexical bindings protect applications that shadow globalThis, Symbol or process.
const key = Symbol.for('quux.otelc.runtime');
let active = false;
const safe = (action, fallback) => {
  if (active) return fallback;
  let owner;
  active = true;
  try { owner = globalThis[key]; return action(owner) ?? fallback; }
  catch { owner?.lose('invalid'); return fallback; }
  finally { active = false; }
};
exports.enter = name => safe(owner => owner?.enter(name), 0);
exports.exit = (token, unwound) => safe(owner => owner?.exit(token, unwound));
exports.enterAsync = (name, id) => safe(owner => owner?.enterAsync(name, id), 0);
exports.attach = (token, previous) => safe(owner => owner?.attach(token, previous), null);
exports.detach = previous => safe(owner => owner?.detach(previous));
exports.suspend = (previous, value) => safe(owner => owner?.suspend(previous, value), value);
exports.lifetimeCreation = () => safe(owner => owner?.parent());
exports.lifetimeType = (constructor, name, resource) => safe(owner => owner?.lifetimes?.type(constructor, name, resource));
exports.lifetimeAdmit = (value, constructor, name, creation) => safe(owner => owner?.lifetimes?.admit(value, constructor, name, creation));
exports.lifetimeFailed = (constructor, name) => safe(owner => owner?.lifetimes?.failedConstruction(constructor, name));
exports.lifetimeReleased = (value, name) => safe(owner => owner?.lifetimes?.released(value, name));
exports.lifetimeReleaseFailed = (value, name) => safe(owner => owner?.lifetimes?.failedRelease(value, name));
