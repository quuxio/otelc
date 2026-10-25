package pipeline

import (
	"context"
	"testing"
)

func TestOnlyOwnedContextSuppressesHTTPInstrumentation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if HTTPInstrumentationSuppressed(ctx) {
		t.Fatal("ordinary context suppressed")
	}
	marked := WithoutHTTPInstrumentation(ctx)
	if !HTTPInstrumentationSuppressed(marked) || HTTPInstrumentationSuppressed(ctx) {
		t.Fatal("owned marker mutated the original context or was lost")
	}
	cancel()
	if marked.Err() != context.Canceled {
		t.Fatal("marker lost application cancellation")
	}
}
