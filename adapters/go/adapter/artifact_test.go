//go:build unix

package adapter

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"

	traceproto "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/protobuf/proto"
)

func artifactPlan(t *testing.T, endpoint string) (string, []byte) {
	t.Helper()
	p := testPlan()
	p.Functions.Include = []string{"main.selected"}
	p.Runtime.ShutdownMS = 2000
	p.Export.MaxQueued = 8
	p.Export.TimeoutMS = 1000
	p.Endpoint = endpoint + "/v1/metrics"
	p.Traces.Enabled, p.Traces.Ratio, p.Traces.MaxActive, p.Traces.MaxSpans = true, 1, 16, 64
	data, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatal(err)
	}
	raw["schema_version"], raw["backend"] = 2, "compile"
	raw["trace_export"] = map[string]any{"endpoint": endpoint + "/v1/traces", "protocol": "http/protobuf", "timeout_ms": 1000}
	data, err = json.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "plan.json")
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	return path, data
}

func captureArtifactRun(t *testing.T, plan, bundle string, baseline bool, argument string) (int, []byte, []byte, error) {
	t.Helper()
	root := t.TempDir()
	in, err := os.Create(filepath.Join(root, "stdin"))
	if err != nil {
		t.Fatal(err)
	}
	defer in.Close()
	if _, err := in.Write([]byte{255, 0, 1}); err != nil {
		t.Fatal(err)
	}
	if _, err := in.Seek(0, 0); err != nil {
		t.Fatal(err)
	}
	out, err := os.Create(filepath.Join(root, "stdout"))
	if err != nil {
		t.Fatal(err)
	}
	defer out.Close()
	stderr, err := os.Create(filepath.Join(root, "stderr"))
	if err != nil {
		t.Fatal(err)
	}
	defer stderr.Close()
	originalIn, originalOut, originalErr := os.Stdin, os.Stdout, os.Stderr
	os.Stdin, os.Stdout, os.Stderr = in, out, stderr
	defer func() { os.Stdin, os.Stdout, os.Stderr = originalIn, originalOut, originalErr }()
	flag := "--run"
	if baseline {
		flag = "--run-baseline"
	}
	code, failure := Run(plan, []string{flag, bundle, argument}, "/missing-sdk-installation")
	stdout, err := os.ReadFile(out.Name())
	if err != nil {
		t.Fatal(err)
	}
	errors, err := os.ReadFile(stderr.Name())
	if err != nil {
		t.Fatal(err)
	}
	return code, stdout, errors, failure
}

func TestRetainedGoBuildAndCompilerFreeReplay(t *testing.T) {
	var lock sync.Mutex
	traces := []*traceproto.ExportTraceServiceRequest{}
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
		}
		lock.Lock()
		defer lock.Unlock()
		requests++
		if r.URL.Path == "/v1/traces" {
			decoded := new(traceproto.ExportTraceServiceRequest)
			if err := proto.Unmarshal(body, decoded); err != nil {
				t.Error(err)
			}
			traces = append(traces, decoded)
		}
		w.WriteHeader(200)
	}))
	defer server.Close()
	plan, originalPlan := artifactPlan(t, server.URL)
	root := t.TempDir()
	manifest := []byte("module example.test/artifact\n\ngo 1.26.0\n")
	source := []byte(`package main
import("fmt";"io";"os";"syscall")
func init(){if path:=os.Getenv("OTELC_ARTIFACT_SENTINEL");path!=""{if err:=os.WriteFile(path,[]byte("init"),0600);err!=nil{panic(err)}}}
func selected(value int)int{return value+1}
func main(){value:=selected(4);if os.Args[1]=="exit17"{os.Exit(17)};if os.Args[1]=="signal"{syscall.Kill(os.Getpid(),syscall.SIGTERM)};data,err:=io.ReadAll(os.Stdin);if err!=nil{panic(err)};fmt.Printf("%d:%s:%x\n",value,os.Args[1],data);fmt.Fprintln(os.Stderr,"original")}
`)
	for name, data := range map[string][]byte{"go.mod": manifest, "main.go": source} {
		if err := os.WriteFile(filepath.Join(root, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	sentinel := filepath.Join(t.TempDir(), "ran")
	t.Setenv("OTELC_ARTIFACT_SENTINEL", sentinel)
	sdk, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	adapter, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	bundle := filepath.Join(t.TempDir(), "bundle")
	func() {
		previous, err := os.Getwd()
		if err != nil {
			t.Fatal(err)
		}
		if err := os.Chdir(root); err != nil {
			t.Fatal(err)
		}
		defer func() {
			if err := os.Chdir(previous); err != nil {
				t.Error(err)
			}
		}()
		code, err := Run(plan, []string{"--build", ".", "--output-dir", bundle}, sdk)
		if err != nil || code != 0 {
			t.Fatal(code, err)
		}
	}()
	if _, err := os.Stat(sentinel); !os.IsNotExist(err) {
		t.Fatal("build-only executed application init/main", err)
	}
	lock.Lock()
	buildRequests := requests
	lock.Unlock()
	if buildRequests != 0 {
		t.Fatal("build-only emitted telemetry", buildRequests)
	}
	for name, want := range map[string][]byte{"go.mod": manifest, "main.go": source} {
		got, err := os.ReadFile(filepath.Join(root, name))
		if err != nil || !bytes.Equal(got, want) {
			t.Fatalf("original %s changed: %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "go.sum")); !os.IsNotExist(err) {
		t.Fatal("original go.sum was created", err)
	}
	if err := buildBundle(plan, ".", bundle, root, sdk, "go", adapter); err == nil {
		t.Fatal("existing artifact directory was overwritten")
	}
	var recipe buildRecipe
	data, err := readBounded(filepath.Join(bundle, "recipe.json"))
	if err != nil || decodeStrict(data, &recipe) != nil {
		t.Fatal("retained compiler recipe unavailable", err)
	}
	if recipe.Inputs[filepath.Join(sdk, "policy/policy.go")].SHA256 == "" || recipe.Inputs[filepath.Join(sdk, "go.mod")].SHA256 == "" || recipe.Inputs[filepath.Join(sdk, "go.sum")].SHA256 == "" {
		t.Fatal("SDK module, lock or imported policy identity missing")
	}
	if err := os.Rename(root, root+"-unavailable"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root + "-unavailable") })
	t.Setenv("PATH", "/missing-go-toolchain")
	t.Setenv("GOROOT", "/missing-go-root")
	t.Setenv("OTELC_GO", "/missing-go")
	report := filepath.Join(t.TempDir(), "report.json")
	t.Setenv("OTELC_REPORT_PATH", report)
	for _, baseline := range []bool{true, false} {
		code, stdout, stderr, err := captureArtifactRun(t, plan, bundle, baseline, "argument")
		if code != 0 || err != nil || string(stdout) != "5:argument:ff0001\n" || string(stderr) != "original\n" {
			t.Fatalf("baseline=%v, status=%d error=%v stdout=%q stderr=%q", baseline, code, err, stdout, stderr)
		}
		if baseline {
			lock.Lock()
			count := requests
			lock.Unlock()
			if count != 0 {
				t.Fatal("ordinary baseline emitted telemetry", count)
			}
		}
	}
	data, err = os.ReadFile(report)
	var health struct {
		Calls    int               `json:"function_calls"`
		Finished bool              `json:"export_finished"`
		Losses   map[string]uint64 `json:"losses"`
	}
	if err != nil || json.Unmarshal(data, &health) != nil || health.Calls != 1 || !health.Finished {
		t.Fatal("missing actual runtime witness", string(data), err)
	}
	for key, value := range health.Losses {
		if value != 0 {
			t.Fatalf("unexpected %s loss: %d", key, value)
		}
	}
	lock.Lock()
	validTrace := len(traces) == 1 && len(traces[0].ResourceSpans) == 1 && len(traces[0].ResourceSpans[0].ScopeSpans) == 1 && len(traces[0].ResourceSpans[0].ScopeSpans[0].Spans) == 1 && traces[0].ResourceSpans[0].ScopeSpans[0].Spans[0].Name == "main.selected"
	lock.Unlock()
	if !validTrace {
		t.Fatal("actual decoded trace witness missing", traces)
	}
	for _, baseline := range []bool{true, false} {
		code, stdout, stderr, err := captureArtifactRun(t, plan, bundle, baseline, "exit17")
		if code != 17 || err != nil || len(stdout)+len(stderr) != 0 {
			t.Fatalf("ordinary status changed: %d %v %q %q", code, err, stdout, stderr)
		}
		_, _, _, failure := captureArtifactRun(t, plan, bundle, baseline, "signal")
		var signal *ChildSignal
		if !errors.As(failure, &signal) || signal.Signal != syscall.SIGTERM {
			t.Fatalf("signal status changed: %v", failure)
		}
	}
	var raw map[string]any
	if err := json.Unmarshal(originalPlan, &raw); err != nil {
		t.Fatal(err)
	}
	raw["metrics"].(map[string]any)["enabled"] = false
	raw["traces"].(map[string]any)["root_sample_ratio"] = 0
	raw["resource"].(map[string]any)["service_name"] = "go-replay-runtime-only"
	changed, err := json.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(plan, changed, 0600); err != nil {
		t.Fatal(err)
	}
	code, stdout, stderr, err := captureArtifactRun(t, plan, bundle, false, "argument")
	if code != 0 || err != nil || string(stdout) != "5:argument:ff0001\n" || string(stderr) != "original\n" {
		t.Fatalf("runtime-only change required rebuilding: %d %v %q %q", code, err, stdout, stderr)
	}
	raw["traces"].(map[string]any)["enabled"] = false
	changed, err = json.Marshal(raw)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(plan, changed, 0600); err != nil {
		t.Fatal(err)
	}
	_, _, _, err = captureArtifactRun(t, plan, bundle, false, "argument")
	if err == nil {
		t.Fatal("changed trace weaving was replayed")
	}
}
