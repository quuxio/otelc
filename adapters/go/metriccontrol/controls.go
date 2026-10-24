// Package metriccontrol gates an owned OpenTelemetry metric pipeline without
// replacing its provider or cached instruments. Tracing is a separate pipeline.
package metriccontrol

import (
	"context"
	"sync/atomic"

	api "go.opentelemetry.io/otel/metric"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// Controls holds independent live switches. Construct it before creating meters,
// even when both switches initially disable metrics.
type Controls struct {
	collection atomic.Bool
	emission   atomic.Bool
	export     chan struct{}
}

// New creates controls with the requested initial state.
func New(collection, emission bool) *Controls {
	c := &Controls{export: make(chan struct{}, 1)}
	c.collection.Store(collection)
	c.emission.Store(emission)
	c.export <- struct{}{}
	return c
}

// CollectionEnabled reports admission of new owned measurements and callbacks.
func (c *Controls) CollectionEnabled() bool { return c.collection.Load() }

// EmissionEnabled reports whether the owned exporter can transmit metrics.
func (c *Controls) EmissionEnabled() bool { return c.emission.Load() }

// SetCollection changes future measurement admission. A measurement or callback
// already in progress may finish; observations recheck admission before recording.
func (c *Controls) SetCollection(enabled bool) { c.collection.Store(enabled) }

func (c *Controls) acquire(ctx context.Context) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-c.export:
		if err := ctx.Err(); err != nil {
			c.release()
			return err
		}
		return nil
	}
}

func (c *Controls) release() { c.export <- struct{}{} }

// SetEmission waits for an in-flight export/flush before changing state. A timed
// out request leaves state unchanged and does not falsely acknowledge a barrier.
func (c *Controls) SetEmission(ctx context.Context, enabled bool) error {
	if err := c.acquire(ctx); err != nil {
		return err
	}
	defer c.release()
	c.emission.Store(enabled)
	return nil
}

// MeterProvider wraps only the scopes selected by owned. It does not install a
// global provider or change existing application-owned SDKs.
func (c *Controls) MeterProvider(base, application api.MeterProvider, owned func(string) bool) api.MeterProvider {
	return provider{MeterProvider: application, instrumented: base, controls: c, owned: owned}
}

type provider struct {
	api.MeterProvider
	instrumented api.MeterProvider
	controls     *Controls
	owned        func(string) bool
}

func (p provider) Meter(name string, options ...api.MeterOption) api.Meter {
	if p.owned(name) {
		return meter{Meter: p.instrumented.Meter(name, options...), controls: p.controls}
	}
	return p.MeterProvider.Meter(name, options...)
}

// Exporter wraps an owned push exporter. Cumulative aggregation retains collected
// measurements while emission is off; downstream exporter preferences cannot
// silently select a lossy delta snapshot for this pipeline.
func (c *Controls) Exporter(base sdk.Exporter) sdk.Exporter {
	return &exporter{Exporter: base, controls: c}
}

type exporter struct {
	sdk.Exporter
	controls    *Controls
	closed      bool // Protected by the controls' export token.
	shutdownErr error
}

func (e *exporter) Temporality(sdk.InstrumentKind) metricdata.Temporality {
	return metricdata.CumulativeTemporality
}

func (e *exporter) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	if err := e.controls.acquire(ctx); err != nil {
		return err
	}
	defer e.controls.release()
	if e.closed {
		return sdk.ErrExporterShutdown
	}
	if !e.controls.EmissionEnabled() {
		return nil
	}
	return e.Exporter.Export(ctx, data)
}

func (e *exporter) ForceFlush(ctx context.Context) error {
	if err := e.controls.acquire(ctx); err != nil {
		return err
	}
	defer e.controls.release()
	if e.closed {
		return sdk.ErrExporterShutdown
	}
	if !e.controls.EmissionEnabled() {
		return nil
	}
	return e.Exporter.ForceFlush(ctx)
}

func (e *exporter) Shutdown(ctx context.Context) error {
	if err := e.controls.acquire(ctx); err != nil {
		return err
	}
	defer e.controls.release()
	if !e.closed {
		e.closed = true
		e.shutdownErr = e.Exporter.Shutdown(ctx)
	}
	return e.shutdownErr
}
