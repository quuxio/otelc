// Optional comment metadata; the original application has no runtime import.
// otelc.instrument
function selected(value: number): number { return value * 3; }
function configured(value: number): number { return value + 7; }
// otelc.exclude
function excluded(value: number): number { return value - 1; }
console.log(selected(4) + configured(10) + excluded(2));
