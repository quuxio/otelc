package bridge

import (
	"context"
	"testing"
)

func TestDefaultAndRegisteredTelemetryBridge(t *testing.T) {
	background := context.Background()
	if ContextWithImplicitTelemetry(background) != background {
		t.Fatal("unregistered bridge changed the input context")
	}
	type key struct{}
	RegisterImplicitTelemetry(func(ctx context.Context) context.Context { return context.WithValue(ctx, key{}, "snapshot") })
	if ContextWithImplicitTelemetry(background).Value(key{}) != "snapshot" {
		t.Fatal("registered bridge was not used")
	}
}
