package httptrace

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
)

func TestNativeEntryConsumesOnlyItsOwnSingleUseToken(t *testing.T) {
	value, _, _ := fixture(t)
	if err := Configure(value.traces, value.meters, value.parent, value.detach); err != nil {
		t.Fatal(err)
	}
	defer configured.Store(nil)
	for _, test := range []struct {
		name    string
		key     any
		enabled func(*http.Request) bool
		other   func(*http.Request) bool
	}{
		{"client", clientKey{}, ClientEnabled, ServerEnabled},
		{"server", serverKey{}, ServerEnabled, ClientEnabled},
	} {
		t.Run(test.name, func(t *testing.T) {
			guard := &atomic.Bool{}
			guard.Store(true)
			request := httptest.NewRequest("GET", "http://example.test/", nil)
			request = request.WithContext(context.WithValue(request.Context(), test.key, guard))
			if !test.other(request) || test.enabled(request) {
				t.Fatal("the immediate native entry did not consume only its own token")
			}
			// Every later native call on the shared context must be admitted,
			// including calls before the outer wrapper has returned.
			var pending sync.WaitGroup
			for range 32 {
				pending.Add(1)
				go func() {
					defer pending.Done()
					if !test.enabled(request) || !test.other(request) {
						t.Error("a concurrent operation inherited native recursion suppression")
					}
				}()
			}
			pending.Wait()
		})
	}
}
