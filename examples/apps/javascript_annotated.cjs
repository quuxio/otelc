// This annotation is optional metadata; no runtime imports are needed.
// otelc.instrument
function selected(value) { return value * 3; }
function configured(value) { return value + 7; }
// otelc.exclude
function excluded(value) { return value - 1; }
console.log(selected(4) + configured(10) + excluded(2));
