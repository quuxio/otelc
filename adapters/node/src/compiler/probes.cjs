// Private lexical bindings protect applications that shadow globalThis, Symbol or process.
const key = Symbol.for('quux.otelc.runtime');
let active = false;
const ownKeys = Reflect.ownKeys;
const define = Object.defineProperty;
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
// All records contain engine-normalised primitive keys, never application values.
exports.lifetimeKey = (keys, site, box, supported, field) => {
  const key = ownKeys(box)[0];
  define(keys, keys.length, { value: { key, site, supported, field }, configurable: true, writable: true, enumerable: true });
  return key;
};
exports.lifetimeType = (constructor, name, keys) => {
  try { return safe(owner => owner?.lifetimes?.type(constructor, name, keys)); }
  // Anonymous methods retain only this empty identity token, not every class key.
  finally { keys.length = 0; }
};
exports.lifetimeAdmit = (value, constructor, name, creation) => safe(owner => owner?.lifetimes?.admit(value, constructor, name, creation));
exports.lifetimeFailed = (constructor, name) => safe(owner => owner?.lifetimes?.failedConstruction(constructor, name));
exports.lifetimeReleased = (value, name, site, constructor) => safe(owner => owner?.lifetimes?.released(value, name, site, constructor));
exports.lifetimeReleaseFailed = (value, name, site, constructor) => safe(owner => owner?.lifetimes?.failedRelease(value, name, site, constructor));
