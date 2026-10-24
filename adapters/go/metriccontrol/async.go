package metriccontrol

import (
	"context"

	api "go.opentelemetry.io/otel/metric"
)

func (m meter) RegisterCallback(callback api.Callback, instruments ...api.Observable) (api.Registration, error) {
	if callback == nil {
		return m.Meter.RegisterCallback(callback, instruments...)
	}
	return m.Meter.RegisterCallback(func(ctx context.Context, observer api.Observer) error {
		if !m.controls.CollectionEnabled() {
			return nil
		}
		return callback(ctx, gatedObserver{observer, m.controls})
	}, instruments...)
}

type gatedObserver struct {
	api.Observer
	controls *Controls
}

func (o gatedObserver) ObserveInt64(instrument api.Int64Observable, value int64, options ...api.ObserveOption) {
	if o.controls.CollectionEnabled() {
		o.Observer.ObserveInt64(instrument, value, options...)
	}
}
func (o gatedObserver) ObserveFloat64(instrument api.Float64Observable, value float64, options ...api.ObserveOption) {
	if o.controls.CollectionEnabled() {
		o.Observer.ObserveFloat64(instrument, value, options...)
	}
}

type int64Observer struct {
	api.Int64Observer
	controls *Controls
}

func (o int64Observer) Observe(value int64, options ...api.ObserveOption) {
	if o.controls.CollectionEnabled() {
		o.Int64Observer.Observe(value, options...)
	}
}
func (c *Controls) callbackInt64(callback api.Int64Callback) api.Int64Callback {
	if callback == nil {
		return nil
	}
	return func(ctx context.Context, observer api.Int64Observer) error {
		if !c.CollectionEnabled() {
			return nil
		}
		return callback(ctx, int64Observer{observer, c})
	}
}

func (m meter) Int64ObservableCounter(name string, options ...api.Int64ObservableCounterOption) (api.Int64ObservableCounter, error) {
	config := api.NewInt64ObservableCounterConfig(options...)
	gated := []api.Int64ObservableCounterOption{api.WithDescription(config.Description()), api.WithUnit(config.Unit())}
	for _, callback := range config.Callbacks() {
		gated = append(gated, api.WithInt64Callback(m.controls.callbackInt64(callback)))
	}
	return m.Meter.Int64ObservableCounter(name, gated...)
}

func (m meter) Int64ObservableUpDownCounter(name string, options ...api.Int64ObservableUpDownCounterOption) (api.Int64ObservableUpDownCounter, error) {
	config := api.NewInt64ObservableUpDownCounterConfig(options...)
	gated := []api.Int64ObservableUpDownCounterOption{api.WithDescription(config.Description()), api.WithUnit(config.Unit())}
	for _, callback := range config.Callbacks() {
		gated = append(gated, api.WithInt64Callback(m.controls.callbackInt64(callback)))
	}
	return m.Meter.Int64ObservableUpDownCounter(name, gated...)
}

func (m meter) Int64ObservableGauge(name string, options ...api.Int64ObservableGaugeOption) (api.Int64ObservableGauge, error) {
	config := api.NewInt64ObservableGaugeConfig(options...)
	gated := []api.Int64ObservableGaugeOption{api.WithDescription(config.Description()), api.WithUnit(config.Unit())}
	for _, callback := range config.Callbacks() {
		gated = append(gated, api.WithInt64Callback(m.controls.callbackInt64(callback)))
	}
	return m.Meter.Int64ObservableGauge(name, gated...)
}

type float64Observer struct {
	api.Float64Observer
	controls *Controls
}

func (o float64Observer) Observe(value float64, options ...api.ObserveOption) {
	if o.controls.CollectionEnabled() {
		o.Float64Observer.Observe(value, options...)
	}
}
func (c *Controls) callbackFloat64(callback api.Float64Callback) api.Float64Callback {
	if callback == nil {
		return nil
	}
	return func(ctx context.Context, observer api.Float64Observer) error {
		if !c.CollectionEnabled() {
			return nil
		}
		return callback(ctx, float64Observer{observer, c})
	}
}

func (m meter) Float64ObservableCounter(name string, options ...api.Float64ObservableCounterOption) (api.Float64ObservableCounter, error) {
	config := api.NewFloat64ObservableCounterConfig(options...)
	gated := []api.Float64ObservableCounterOption{api.WithDescription(config.Description()), api.WithUnit(config.Unit())}
	for _, callback := range config.Callbacks() {
		gated = append(gated, api.WithFloat64Callback(m.controls.callbackFloat64(callback)))
	}
	return m.Meter.Float64ObservableCounter(name, gated...)
}

func (m meter) Float64ObservableUpDownCounter(name string, options ...api.Float64ObservableUpDownCounterOption) (api.Float64ObservableUpDownCounter, error) {
	config := api.NewFloat64ObservableUpDownCounterConfig(options...)
	gated := []api.Float64ObservableUpDownCounterOption{api.WithDescription(config.Description()), api.WithUnit(config.Unit())}
	for _, callback := range config.Callbacks() {
		gated = append(gated, api.WithFloat64Callback(m.controls.callbackFloat64(callback)))
	}
	return m.Meter.Float64ObservableUpDownCounter(name, gated...)
}

func (m meter) Float64ObservableGauge(name string, options ...api.Float64ObservableGaugeOption) (api.Float64ObservableGauge, error) {
	config := api.NewFloat64ObservableGaugeConfig(options...)
	gated := []api.Float64ObservableGaugeOption{api.WithDescription(config.Description()), api.WithUnit(config.Unit())}
	for _, callback := range config.Callbacks() {
		gated = append(gated, api.WithFloat64Callback(m.controls.callbackFloat64(callback)))
	}
	return m.Meter.Float64ObservableGauge(name, gated...)
}
