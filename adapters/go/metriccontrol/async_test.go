package metriccontrol

import (
	"context"
	"errors"
	"testing"

	api "go.opentelemetry.io/otel/metric"
	sdk "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

func TestObservableCallbacksAndCachedRegistrationsFollowCollectionOnly(t *testing.T) {
	c := New(false, false)
	provider, reader, applicationReader := setup(t, c)
	meter := provider.Meter("owned", api.WithInstrumentationVersion("probe-v1"))
	calls, appCalls := 0, 0
	integer := func(ctx context.Context, o api.Int64Observer) error { calls++; o.Observe(7); return nil }
	floating := func(ctx context.Context, o api.Float64Observer) error { calls++; o.Observe(7); return nil }
	_, a := meter.Int64ObservableCounter("i.obs.counter", api.WithDescription("observed"), api.WithUnit("task"), api.WithInt64Callback(integer))
	_, b := meter.Int64ObservableUpDownCounter("i.obs.updown", api.WithInt64Callback(integer))
	_, d := meter.Int64ObservableGauge("i.obs.gauge", api.WithInt64Callback(integer))
	_, e := meter.Float64ObservableCounter("f.obs.counter", api.WithFloat64Callback(floating))
	_, f := meter.Float64ObservableUpDownCounter("f.obs.updown", api.WithFloat64Callback(floating))
	_, g := meter.Float64ObservableGauge("f.obs.gauge", api.WithFloat64Callback(floating))
	for _, err := range []error{a, b, d, e, f, g} {
		if err != nil {
			t.Fatal(err)
		}
	}
	i, _ := meter.Int64ObservableGauge("i.registered")
	j, _ := meter.Float64ObservableGauge("f.registered")
	registration, err := meter.RegisterCallback(func(ctx context.Context, o api.Observer) error {
		calls++
		o.ObserveInt64(i, 7)
		o.ObserveFloat64(j, 7)
		return nil
	}, i, j)
	if err != nil {
		t.Fatal(err)
	}
	_, err = provider.Meter("application").Int64ObservableGauge("app.callback", api.WithInt64Callback(func(ctx context.Context, o api.Int64Observer) error { appCalls++; o.Observe(5); return nil }))
	if err != nil {
		t.Fatal(err)
	}
	first := values(collect(t, reader))
	application := values(collect(t, applicationReader))
	if calls != 0 || appCalls != 1 || len(first) != 0 || application["app.callback"] != 5 {
		t.Fatal(calls, appCalls, first)
	}
	c.SetCollection(true)
	data := collect(t, reader)
	if calls != 7 || len(values(data)) != 8 {
		t.Fatal(calls, values(data))
	}
	for name, value := range values(data) {
		if name != "app.callback" && value != 7 {
			t.Fatal(name, value)
		}
	}
	found := false
	for _, scope := range data.ScopeMetrics {
		if scope.Scope.Name != "owned" {
			continue
		}
		if scope.Scope.Version != "probe-v1" {
			t.Fatal(scope.Scope)
		}
		for _, metric := range scope.Metrics {
			if metric.Name == "i.obs.counter" {
				found = true
				if metric.Unit != "task" || metric.Description != "observed" {
					t.Fatal(metric)
				}
			}
		}
	}
	if !found {
		t.Fatal("observable metadata missing")
	}
	c.SetCollection(false)
	disabled := values(collect(t, reader))
	if calls != 7 || len(disabled) != 0 {
		t.Fatal("stale owned observations", calls, disabled)
	}
	if err := registration.Unregister(); err != nil {
		t.Fatal(err)
	}
	c.SetCollection(true)
	restored := values(collect(t, reader))
	if calls != 13 || len(restored) != 6 {
		t.Fatal(calls, restored)
	}
}

func TestObservationsRecheckCollectionIfCallbackDisablesIt(t *testing.T) {
	for _, kind := range []string{"integer", "floating", "registered-int", "registered-float"} {
		t.Run(kind, func(t *testing.T) {
			c := New(true, true)
			provider, reader, _ := setup(t, c)
			meter := provider.Meter("owned")
			var err error
			switch kind {
			case "integer":
				_, err = meter.Int64ObservableGauge("value", api.WithInt64Callback(func(ctx context.Context, o api.Int64Observer) error {
					c.SetCollection(false)
					o.Observe(99)
					return nil
				}))
			case "floating":
				_, err = meter.Float64ObservableGauge("value", api.WithFloat64Callback(func(ctx context.Context, o api.Float64Observer) error {
					c.SetCollection(false)
					o.Observe(99)
					return nil
				}))
			case "registered-int":
				instrument, e := meter.Int64ObservableGauge("value")
				if e != nil {
					t.Fatal(e)
				}
				_, err = meter.RegisterCallback(func(ctx context.Context, o api.Observer) error {
					c.SetCollection(false)
					o.ObserveInt64(instrument, 99)
					return nil
				}, instrument)
			default:
				instrument, e := meter.Float64ObservableGauge("value")
				if e != nil {
					t.Fatal(e)
				}
				_, err = meter.RegisterCallback(func(ctx context.Context, o api.Observer) error {
					c.SetCollection(false)
					o.ObserveFloat64(instrument, 99)
					return nil
				}, instrument)
			}
			if err != nil {
				t.Fatal(err)
			}
			if got := values(collect(t, reader)); len(got) != 0 {
				t.Fatal(got)
			}
		})
	}
}

func TestCallbackErrorsAndNilRegistrationKeepSDKBehaviour(t *testing.T) {
	c := New(true, true)
	provider, reader, _ := setup(t, c)
	meter := provider.Meter("owned")
	sentinel := errors.New("callback failed")
	_, err := meter.Int64ObservableGauge("failed", api.WithInt64Callback(func(context.Context, api.Int64Observer) error { return sentinel }))
	if err != nil {
		t.Fatal(err)
	}
	if err := reader.Collect(context.Background(), &metricdata.ResourceMetrics{}); !errors.Is(err, sentinel) {
		t.Fatal(err)
	}
	c.SetCollection(false)
	if err := reader.Collect(context.Background(), &metricdata.ResourceMetrics{}); err != nil {
		t.Fatal(err)
	}
	// An invalid registration still reaches the SDK's own validation path.
	registration, err := meter.RegisterCallback(nil)
	base, baseErr := sdk.NewMeterProvider().Meter("base").RegisterCallback(nil)
	if (registration == nil) != (base == nil) || (err == nil) != (baseErr == nil) {
		t.Fatal(registration, err, base, baseErr)
	}
	if registration != nil {
		if err := registration.Unregister(); err != nil {
			t.Fatal(err)
		}
	}
	if base != nil {
		if err := base.Unregister(); err != nil {
			t.Fatal(err)
		}
	}
}
