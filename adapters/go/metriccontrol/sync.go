package metriccontrol

import (
	"context"

	api "go.opentelemetry.io/otel/metric"
)

type meter struct {
	api.Meter
	controls *Controls
}

func (m meter) Int64Counter(name string, options ...api.Int64CounterOption) (api.Int64Counter, error) {
	instrument, err := m.Meter.Int64Counter(name, options...)
	if instrument == nil {
		return nil, err
	}
	return int64Counter{instrument, m.controls}, err
}

type int64Counter struct {
	api.Int64Counter
	controls *Controls
}

func (i int64Counter) Add(ctx context.Context, value int64, options ...api.AddOption) {
	if i.controls.CollectionEnabled() {
		i.Int64Counter.Add(ctx, value, options...)
	}
}

func (m meter) Int64UpDownCounter(name string, options ...api.Int64UpDownCounterOption) (api.Int64UpDownCounter, error) {
	instrument, err := m.Meter.Int64UpDownCounter(name, options...)
	if instrument == nil {
		return nil, err
	}
	return int64UpDownCounter{instrument, m.controls}, err
}

type int64UpDownCounter struct {
	api.Int64UpDownCounter
	controls *Controls
}

func (i int64UpDownCounter) Add(ctx context.Context, value int64, options ...api.AddOption) {
	if i.controls.CollectionEnabled() {
		i.Int64UpDownCounter.Add(ctx, value, options...)
	}
}

func (m meter) Int64Histogram(name string, options ...api.Int64HistogramOption) (api.Int64Histogram, error) {
	instrument, err := m.Meter.Int64Histogram(name, options...)
	if instrument == nil {
		return nil, err
	}
	return int64Histogram{instrument, m.controls}, err
}

type int64Histogram struct {
	api.Int64Histogram
	controls *Controls
}

func (i int64Histogram) Record(ctx context.Context, value int64, options ...api.RecordOption) {
	if i.controls.CollectionEnabled() {
		i.Int64Histogram.Record(ctx, value, options...)
	}
}

func (m meter) Int64Gauge(name string, options ...api.Int64GaugeOption) (api.Int64Gauge, error) {
	instrument, err := m.Meter.Int64Gauge(name, options...)
	if instrument == nil {
		return nil, err
	}
	return int64Gauge{instrument, m.controls}, err
}

type int64Gauge struct {
	api.Int64Gauge
	controls *Controls
}

func (i int64Gauge) Record(ctx context.Context, value int64, options ...api.RecordOption) {
	if i.controls.CollectionEnabled() {
		i.Int64Gauge.Record(ctx, value, options...)
	}
}

func (m meter) Float64Counter(name string, options ...api.Float64CounterOption) (api.Float64Counter, error) {
	instrument, err := m.Meter.Float64Counter(name, options...)
	if instrument == nil {
		return nil, err
	}
	return float64Counter{instrument, m.controls}, err
}

type float64Counter struct {
	api.Float64Counter
	controls *Controls
}

func (i float64Counter) Add(ctx context.Context, value float64, options ...api.AddOption) {
	if i.controls.CollectionEnabled() {
		i.Float64Counter.Add(ctx, value, options...)
	}
}

func (m meter) Float64UpDownCounter(name string, options ...api.Float64UpDownCounterOption) (api.Float64UpDownCounter, error) {
	instrument, err := m.Meter.Float64UpDownCounter(name, options...)
	if instrument == nil {
		return nil, err
	}
	return float64UpDownCounter{instrument, m.controls}, err
}

type float64UpDownCounter struct {
	api.Float64UpDownCounter
	controls *Controls
}

func (i float64UpDownCounter) Add(ctx context.Context, value float64, options ...api.AddOption) {
	if i.controls.CollectionEnabled() {
		i.Float64UpDownCounter.Add(ctx, value, options...)
	}
}

func (m meter) Float64Histogram(name string, options ...api.Float64HistogramOption) (api.Float64Histogram, error) {
	instrument, err := m.Meter.Float64Histogram(name, options...)
	if instrument == nil {
		return nil, err
	}
	return float64Histogram{instrument, m.controls}, err
}

type float64Histogram struct {
	api.Float64Histogram
	controls *Controls
}

func (i float64Histogram) Record(ctx context.Context, value float64, options ...api.RecordOption) {
	if i.controls.CollectionEnabled() {
		i.Float64Histogram.Record(ctx, value, options...)
	}
}

func (m meter) Float64Gauge(name string, options ...api.Float64GaugeOption) (api.Float64Gauge, error) {
	instrument, err := m.Meter.Float64Gauge(name, options...)
	if instrument == nil {
		return nil, err
	}
	return float64Gauge{instrument, m.controls}, err
}

type float64Gauge struct {
	api.Float64Gauge
	controls *Controls
}

func (i float64Gauge) Record(ctx context.Context, value float64, options ...api.RecordOption) {
	if i.controls.CollectionEnabled() {
		i.Float64Gauge.Record(ctx, value, options...)
	}
}
