package adapter

import (
	"bytes"
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"io.quux.otelc/go/policy"
)

func testPlan() policy.Plan {
	p := policy.Plan{Language: "go", Available: true, Endpoint: "http://127.0.0.1:1/v1/metrics"}
	p.Sources.Include = []string{"(?-u).*"}
	p.Functions.Include = []string{"(?-u).*"}
	p.Functions.Exclude = []string{"(?-u).*excluded.*", "(?-u).*main"}
	p.Runtime.MaxFunctions = 64
	p.Runtime.MaxActive = 64
	p.Runtime.ShutdownMS = 500
	p.Metrics.Enabled = true
	p.Metrics.Bounds = []float64{.001, .01, 1}
	p.Export.IntervalMS = 60000
	p.Export.TimeoutMS = 100
	p.Resource.Name = "go-transform-test"
	p.Annotations.Read = true
	return p
}
func TestTransformSelectionAnnotationsAndOriginalLineMapping(t *testing.T) {
	original := []byte("package main\nimport \"fmt\"\nvar __quux_otelc=1\n// otelc.instrument\nfunc chosen[T any](value T) T { return value }\n// otelc.exclude\nfunc excluded(){}\ntype Order[T any] struct{}\nfunc (order *Order[T]) method(){}\nfunc main(){fmt.Println(chosen(3));func(){}()}\n")
	p := testPlan()
	p.Annotations.Inject = true
	generated, functions, err := Transform("/original/app.go", original, "app", p, true)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(generated, []byte("__quux_otelc_")) || !bytes.Contains(generated, []byte("//line /original/app.go:1")) {
		t.Fatal("unique binder or line mapping absent")
	}
	if _, err = parser.ParseFile(token.NewFileSet(), "generated.go", generated, parser.ParseComments); err != nil {
		t.Fatal(err)
	}
	if len(functions) != 5 || functions[1].Selected || functions[3].Selected || !functions[2].Selected || functions[2].Name != "app.Order.method" {
		t.Fatalf("bad selection %+v", functions)
	}
	if _, _, err = Transform("again.go", generated, "app", p, true); err == nil {
		t.Fatal("double instrumentation allowed")
	}
	selected := policy.Selection{Include: []string{"none"}}
	p.Functions = selected
	_, functions, err = Transform("app.go", original, "app", p, true)
	if err != nil || !functions[0].Selected {
		t.Fatal("annotation did not opt in")
	}
	p.Annotations.Read = false
	_, functions, err = Transform("app.go", original, "app", p, true)
	if err != nil || functions[0].Selected {
		t.Fatal("annotation could not be disabled")
	}
	_, functions, err = Transform("app.go", original, "app", p, false)
	if err != nil {
		t.Fatal(err)
	}
	for _, fn := range functions {
		if fn.Selected {
			t.Fatal("excluded source admitted")
		}
	}
	empty := []byte("package library\nfunc plain(){}\n")
	plain, _, err := Transform("library.go", empty, "library", p, false)
	if err != nil || !bytes.Equal(plain, empty) {
		t.Fatal("unselected library transformed")
	}
	p.Annotations.Read = true
	if _, _, err = Transform("bad.go", []byte("package main\n// otelc.unknown\nfunc main(){}"), "main", p, true); err == nil {
		t.Fatal("invalid annotation accepted")
	}
	if _, _, err = Transform("bad.go", []byte("not Go"), "bad", p, true); err == nil {
		t.Fatal("invalid Go accepted")
	}
	if receiver(&ast.BasicLit{Kind: token.INT, Value: "1"}) != "<receiver>" {
		t.Fatal("receiver fallback")
	}
	p.Functions.Include = []string{".*"}
	_, callbacks, err := Transform("callback.go", []byte("package library\nvar callback=func()int{return 3}\n"), "callback", p, true)
	if err != nil || len(callbacks) != 1 || !callbacks[0].Selected {
		t.Fatal("global callback missing", callbacks, err)
	}
}

func TestOverlayBuildPreservesModuleSourcesRecoverAndArguments(t *testing.T) {
	sdkRoot, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	manifest := []byte("module example.test/app\n\ngo 1.26.0\n")
	os.WriteFile(filepath.Join(root, "go.mod"), manifest, 0600)
	source := []byte(`package main
import("fmt";"os";"sync")
func recursive(n int)int{if n==0{return 0};return 1+recursive(n-1)}
func recovered()(n int){defer func(){if recover()!=nil{n=7}}();panic("same")}
func escaping(){panic("same")}
func deferred()(n int){defer func(){n++}();return 4}
func selected(n int)int{return n*2}
func main(){var wg sync.WaitGroup;ch:=make(chan int,2);for _,n:=range []int{2,3}{wg.Go(func(){ch<-selected(n)})};wg.Wait();close(ch);n:=recursive(3)+recovered()+deferred();for value:=range ch{n+=value};func(){defer func(){if recover()!="same"{panic("changed")}}();escaping()}();fmt.Println(n,os.Args[1])}
`)
	filename := filepath.Join(root, "main.go")
	os.WriteFile(filename, source, 0600)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer server.Close()
	p := testPlan()
	p.Endpoint = server.URL
	p.Functions.Exclude = append(p.Functions.Exclude, ".*<anonymous>.*")
	scratch := t.TempDir()
	binary, err := Build(p, ".", root, scratch, sdkRoot, "go")
	if err != nil {
		t.Fatal(err)
	}
	planFile := filepath.Join(scratch, "plan.json")
	data, _ := json.Marshal(p)
	os.WriteFile(planFile, data, 0600)
	report := filepath.Join(scratch, "report.json")
	cmd := exec.Command(binary, "argument")
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "OTELC_GO_PLAN="+planFile, "OTELC_REPORT_PATH="+report)
	output, err := cmd.CombinedOutput()
	if err != nil || string(output) != "25 argument\n" {
		t.Fatal(string(output), err)
	}
	plain, err := command("go", root, "run", ".", "argument").CombinedOutput()
	if err != nil || !bytes.Equal(plain, output) {
		t.Fatal("baseline changed", string(plain), err)
	}
	var result struct {
		Calls     uint64 `json:"function_calls"`
		Finished  bool   `json:"export_finished"`
		Functions map[string]struct{ Count, Unwinds uint64 }
		Losses    map[string]uint64
	}
	data, err = os.ReadFile(report)
	if err != nil {
		t.Fatal(err)
	}
	json.Unmarshal(data, &result)
	if result.Calls != 9 || !result.Finished || result.Functions["main.escaping"].Unwinds != 1 || result.Functions["main.recovered"].Unwinds != 0 {
		t.Fatalf("wrong observations %+v", result)
	}
	for _, loss := range result.Losses {
		if loss != 0 {
			t.Fatal("lost observations", result)
		}
	}
	after, _ := os.ReadFile(filename)
	afterMod, _ := os.ReadFile(filepath.Join(root, "go.mod"))
	if !bytes.Equal(after, source) || !bytes.Equal(afterMod, manifest) {
		t.Fatal("original application was edited")
	}
	if _, err = os.Stat(filepath.Join(root, "go.sum")); !os.IsNotExist(err) {
		t.Fatal("original sum was created")
	}
	if _, err = Build(p, "missing.go", root, t.TempDir(), sdkRoot, "go"); err == nil {
		t.Fatal("missing target accepted")
	}
	invalidRoot := t.TempDir()
	if _, err = Build(p, ".", invalidRoot, t.TempDir(), sdkRoot, "go"); err == nil {
		t.Fatal("module-less package accepted")
	}
	os.WriteFile(filepath.Join(invalidRoot, "bad.go"), []byte("package library\nfunc x(){}"), 0600)
	previous, _ := os.Getwd()
	os.Chdir(invalidRoot)
	defer os.Chdir(previous)
	if _, err = Build(p, "bad.go", invalidRoot, t.TempDir(), sdkRoot, "go"); err == nil {
		t.Fatal("library execution accepted")
	}
}
func TestDoctorInspectAndLaunchValidation(t *testing.T) {
	root, _ := os.Getwd()
	if err := Doctor("go", root); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"-overlay=bad", "-modfile=bad", "-toolexec=bad", "-buildmode=plugin", "-mod=vendor"} {
		t.Setenv("GOFLAGS", name)
		if err := Doctor("go", root); err == nil {
			t.Fatal("conflicting build flags accepted")
		}
	}
	t.Setenv("GOFLAGS", "")
	t.Setenv("GODEBUG", "panicnil=1")
	if err := Doctor("go", root); err == nil {
		t.Fatal("legacy nil panic accepted")
	}
	t.Setenv("GODEBUG", "panicnil=0")
	if err := Doctor("/missing-go", root); err == nil {
		t.Fatal("missing tool accepted")
	}
	plan := testPlan()
	dir := t.TempDir()
	file := filepath.Join(dir, "plan.json")
	data, _ := json.Marshal(plan)
	os.WriteFile(file, data, 0600)
	sdkRoot, _ := filepath.Abs("..")
	if _, err := Run("missing", nil, sdkRoot); err == nil {
		t.Fatal("missing plan accepted")
	}
	if _, err := Run(file, []string{"--doctor"}, sdkRoot); err != nil {
		t.Fatal(err)
	}
	source := filepath.Join(dir, "app.go")
	os.WriteFile(source, []byte("package main\nfunc main(){}"), 0600)
	if _, err := Run(file, []string{"--inspect", source, "--json"}, sdkRoot); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{nil, {"--invalid"}, {"--inspect", source, "--bad"}, {"--inspect", "missing.go"}} {
		if _, err := Run(file, args, sdkRoot); err == nil {
			t.Fatal("invalid launch accepted", args)
		}
	}
	if _, err := output("go", root, "invalid-command"); err == nil {
		t.Fatal("invalid go command accepted")
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	defer server.Close()
	plan.Endpoint = server.URL
	data, _ = json.Marshal(plan)
	os.WriteFile(file, data, 0600)
	previous, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(previous)
	if code, err := Run(file, []string{"app.go"}, sdkRoot); err != nil || code != 0 {
		t.Fatal(code, err)
	}
	os.WriteFile(source, []byte("package main\nimport \"os\"\nfunc main(){os.Exit(17)}"), 0600)
	if code, err := Run(file, []string{"app.go"}, sdkRoot); err != nil || code != 17 {
		t.Fatal("exit status changed", code, err)
	}
}
