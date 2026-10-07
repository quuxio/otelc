// Package policy consumes the common, externally resolved instrumentation policy.
package policy

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strings"
)

type Selection struct {
	Include []string `json:"include"`
	Exclude []string `json:"exclude"`
}
type Plan struct {
	Language    string    `json:"language"`
	Available   bool      `json:"execution_available"`
	Sources     Selection `json:"source_matchers"`
	Functions   Selection `json:"function_matchers"`
	Annotations struct {
		Read   bool `json:"read_existing"`
		Inject bool `json:"inject_generated"`
	} `json:"annotations"`
	Runtime struct {
		MaxFunctions int     `json:"max_functions"`
		MaxActive    int     `json:"max_active_calls"`
		ShutdownMS   int     `json:"shutdown_timeout_ms"`
		Socket       *string `json:"control_socket"`
	} `json:"runtime"`
	Metrics struct {
		Enabled bool      `json:"enabled"`
		Bounds  []float64 `json:"histogram_boundaries_seconds"`
	} `json:"metrics"`
	Resource struct {
		Name       string            `json:"service_name"`
		Version    string            `json:"service_version"`
		Attributes map[string]string `json:"attributes"`
	} `json:"resource"`
	Export struct {
		IntervalMS int `json:"interval_ms"`
		TimeoutMS  int `json:"timeout_ms"`
		MaxQueued  int `json:"max_queued_batches"`
	} `json:"export"`
	Traces struct {
		Enabled   bool    `json:"enabled"`
		Ratio     float64 `json:"root_sample_ratio"`
		MaxActive int     `json:"max_active_traces"`
		MaxSpans  int     `json:"max_spans_per_trace"`
	} `json:"traces"`
	TraceExport *struct {
		Endpoint  string `json:"endpoint"`
		Protocol  string `json:"protocol"`
		TimeoutMS int    `json:"timeout_ms"`
	} `json:"trace_export"`
	Endpoint string `json:"metrics_endpoint"`
}

func Load(path string) (Plan, error) {
	var p Plan
	data, err := os.ReadFile(path)
	if err != nil {
		return p, err
	}
	if err = json.Unmarshal(data, &p); err != nil {
		return p, err
	}
	if p.Language != "go" || !p.Available {
		return p, fmt.Errorf("go adapter requires an executable Go policy")
	}
	if p.Runtime.MaxFunctions < 1 || p.Runtime.MaxActive < 1 || p.Runtime.ShutdownMS < 1 || p.Export.IntervalMS < 1 || p.Export.TimeoutMS < 1 {
		return p, fmt.Errorf("invalid resolved Go runtime limits")
	}
	return p, nil
}
func (s Selection) Accept(name string, annotated bool) bool {
	runes := make([]rune, len([]byte(name)))
	for i, value := range []byte(name) {
		runes[i] = rune(value)
	}
	match := func(patterns []string) bool {
		for _, pattern := range patterns {
			re, err := regexp.Compile("(?s)^(?:" + strings.ReplaceAll(pattern, "(?-u)", "") + ")$")
			if err == nil && re.MatchString(string(runes)) {
				return true
			}
		}
		return false
	}
	return !match(s.Exclude) && (annotated || match(s.Include))
}
func LegacyNilPanic(value string) bool {
	result := false
	for _, entry := range strings.Split(value, ",") {
		if strings.HasPrefix(entry, "panicnil=") {
			result = entry == "panicnil=1"
		}
	}
	return result
}
