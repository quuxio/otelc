package pipeline

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"time"

	api "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/metric/noop"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/resource"
	"io.quux.otelc/go/metriccontrol"
)

// Scope is reserved for the generated otelc probes in the owned provider.
const Scope = "io.quux.otelc.go"

// Metrics keeps the same provider, instruments and cumulative aggregation alive
// through all four control states. Export failures remain errors and retry on
// the next collection; no failed snapshot is marked successfully transmitted.
type Metrics struct {
	Controls *metriccontrol.Controls
	Provider api.MeterProvider
	base     *sdk.MeterProvider
	reader   *sdk.ManualReader
	exporter sdk.Exporter
	sender   *metricExporter
	health   counters
	token    chan struct{}
	worker   worker
	mu       sync.Mutex
	closed   bool
	closeErr error
}

type metricExporter struct {
	sdk.Exporter
	health *counters
	closed atomic.Bool
}

func (e *metricExporter) Export(ctx context.Context, data *metricdata.ResourceMetrics) error {
	e.health.attempts.Add(1)
	err := e.Exporter.Export(ctx, data)
	if err != nil {
		e.health.failed.Add(1)
	} else {
		e.health.succeeded.Add(1)
	}
	return err
}

func (e *metricExporter) Shutdown(ctx context.Context) error {
	e.closed.Store(true)
	return e.Exporter.Shutdown(ctx)
}

// NewMetrics owns the supplied exporter and an isolated SDK. A zero interval
// uses explicit Flush only. No global meter provider is replaced.
func NewMetrics(controls *metriccontrol.Controls, exporter sdk.Exporter, res *resource.Resource,
	interval, timeout time.Duration) (*Metrics, error) {
	if controls == nil || exporter == nil || interval < 0 || timeout <= 0 {
		return nil, errors.New("invalid owned metric pipeline settings")
	}
	m := &Metrics{Controls: controls, token: make(chan struct{}, 1)}
	m.token <- struct{}{}
	m.sender = &metricExporter{Exporter: exporter, health: &m.health}
	m.exporter = controls.Exporter(m.sender)
	m.reader = sdk.NewManualReader(sdk.WithTemporalitySelector(m.exporter.Temporality),
		sdk.WithAggregationSelector(m.exporter.Aggregation))
	m.base = sdk.NewMeterProvider(sdk.WithReader(m.reader), sdk.WithResource(res))
	m.Provider = controls.MeterProvider(m.base, noop.NewMeterProvider(), func(scope string) bool {
		return scope == Scope || scope == httpScope
	})
	m.worker = start(interval, timeout, m.Flush)
	return m, nil
}

func (m *Metrics) flush(ctx context.Context) error {
	if !m.Controls.CollectionEnabled() {
		return nil
	}
	data := &metricdata.ResourceMetrics{}
	if err := m.reader.Collect(ctx, data); err != nil {
		return err
	}
	if !m.Controls.CollectionEnabled() {
		return nil
	}
	return m.exporter.Export(ctx, data)
}

// Flush serialises collection/export and returns failures to its caller. The
// controls' own emission barrier also covers this path and periodic exports.
func (m *Metrics) Flush(ctx context.Context) error {
	if err := acquire(ctx, m.token); err != nil {
		return err
	}
	defer func() { m.token <- struct{}{} }()
	m.mu.Lock()
	closed := m.closed
	m.mu.Unlock()
	if closed {
		return sdk.ErrExporterShutdown
	}
	return m.flush(ctx)
}

// Health reports raw export outcomes, including while collection/emission is off.
func (m *Metrics) Health() Health {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.health.snapshot(0, m.closed)
}

// Shutdown cancels periodic work, drains once and closes owned resources. A
// caller whose deadline expires before admission can retry shutdown later.
func (m *Metrics) Shutdown(ctx context.Context) error {
	m.worker.cancel()
	if err := acquire(ctx, m.token); err != nil {
		return err
	}
	defer func() { m.token <- struct{}{} }()
	m.mu.Lock()
	if m.closed && m.sender.closed.Load() {
		err := m.closeErr
		m.mu.Unlock()
		return err
	}
	wasClosed := m.closed
	m.closed = true
	m.mu.Unlock()
	var drainErr error
	if !wasClosed {
		drainErr = errors.Join(m.flush(ctx), m.base.Shutdown(ctx))
	}
	// If cancellation prevents the emission gate from admitting shutdown,
	// preserve that state so a later call can still close the exporter.
	err := errors.Join(drainErr, m.exporter.Shutdown(ctx))
	m.mu.Lock()
	m.closeErr = errors.Join(m.closeErr, err)
	result := m.closeErr
	m.mu.Unlock()
	return result
}
