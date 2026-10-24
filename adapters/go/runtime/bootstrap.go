package runtime

import (
	"errors"
	"os"
	"sync"

	"go.opentelemetry.io/otel/trace"
)

type loader struct {
	once              sync.Once
	directory, digest string
	runtime           *Runtime
	err               error
}

var ownedRuntime loader

// FromEnvironment is used only by private compiler-generated package helpers.
// The first import initialises one SDK; subsequent instrumented packages share
// its providers/control endpoint rather than install a provider per package.
func FromEnvironment(parent func() trace.Span, detach func(trace.Span) bool) (*Runtime, error) {
	return ownedRuntime.get(os.Getenv("OTELC_GO_RUNTIME"), os.Getenv("OTELC_GO_CONFIG_SHA256"), parent, detach)
}

func (l *loader) get(directory, digest string, parent func() trace.Span, detach func(trace.Span) bool) (*Runtime, error) {
	l.once.Do(func() {
		l.directory, l.digest = directory, digest
		var config Config
		config, l.err = Load(directory, digest)
		if l.err == nil {
			l.runtime, l.err = New(config, directory, parent, detach)
		}
	})
	if l.directory != directory || l.digest != digest {
		return nil, errors.New("Go runtime bootstrap binding changed")
	}
	return l.runtime, l.err
}
