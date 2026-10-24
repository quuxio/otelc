package pipeline

import "context"

type httpSuppressionKey struct{}

// WithoutHTTPInstrumentation marks owned SDK requests without relying on an
// application-visible URL or User-Agent convention.
func WithoutHTTPInstrumentation(ctx context.Context) context.Context {
	return context.WithValue(ctx, httpSuppressionKey{}, true)
}

// HTTPInstrumentationSuppressed recognises only the owned transport marker.
func HTTPInstrumentationSuppressed(ctx context.Context) bool {
	return ctx.Value(httpSuppressionKey{}) == true
}
