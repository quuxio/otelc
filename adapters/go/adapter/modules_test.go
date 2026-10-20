package adapter

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestModuleIdentityPreservation(t *testing.T) {
	dependency := moduleIdentity{Path: "example.test/dep", Version: "v1.0.0", Sum: "h1:archive", GoModSum: "h1:manifest", ModHash: "manifest-bytes", GoVersion: "1.26"}
	local := moduleIdentity{Path: "../local", Dir: "/original/local", ModHash: "local-manifest"}
	tests := []struct {
		name   string
		change func(*moduleIdentity)
	}{
		{"version", func(m *moduleIdentity) { m.Version = "v1.1.0" }},
		{"path", func(m *moduleIdentity) { m.Path = "example.test/other" }},
		{"archive checksum", func(m *moduleIdentity) { m.Sum = "h1:changed" }},
		{"manifest checksum", func(m *moduleIdentity) { m.GoModSum = "h1:changed" }},
		{"manifest bytes", func(m *moduleIdentity) { m.ModHash = "changed" }},
		{"dependency language", func(m *moduleIdentity) { m.GoVersion = "1.27" }},
		{"replacement introduced", func(m *moduleIdentity) { m.Replace = &local }},
		{"became main", func(m *moduleIdentity) { m.Main = true }},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			after := dependency
			test.change(&after)
			if sameModuleIdentity(dependency, after) {
				t.Fatal("changed application dependency accepted")
			}
		})
	}
	dependency.Replace = &local
	other := local
	other.Dir = "/other/local"
	after := dependency
	after.Replace = &other
	if sameModuleIdentity(dependency, after) {
		t.Fatal("different local replacement accepted")
	}
	after.Replace = nil
	if sameModuleIdentity(dependency, after) {
		t.Fatal("removed local replacement accepted")
	}
	after = dependency
	if !sameModuleIdentity(dependency, after) {
		t.Fatal("unchanged local replacement refused")
	}
	before := moduleSnapshot{Settings: moduleSettings{Go: "1.26"}, Modules: map[string]moduleIdentity{"example.test/dep": dependency}}
	resolved := moduleSnapshot{Settings: moduleSettings{Go: "1.26.0"}, Modules: map[string]moduleIdentity{"example.test/dep": dependency, "io.quux.otelc/go": {Path: "io.quux.otelc/go", Version: "v0.0.0"}}}
	if err := preserveModules(before, resolved); err != nil {
		t.Fatal("new SDK dependency or equivalent patch spelling refused", err)
	}
	delete(resolved.Modules, "example.test/dep")
	if err := preserveModules(before, resolved); err == nil || !strings.Contains(err.Error(), "example.test/dep") {
		t.Fatal("removed application dependency accepted", err)
	}
	for _, settings := range []moduleSettings{{Go: "1.27"}, {Go: "1.26", Toolchain: "go1.27.2"}, {Go: "1.26", Godebug: []struct{ Key, Value string }{{"panicnil", "1"}}}} {
		if err := preserveModules(before, moduleSnapshot{Settings: settings}); err == nil {
			t.Fatal("changed application language/toolchain/defaults accepted", settings)
		}
	}
	if languageVersion("") != "go1.16" {
		t.Fatal("missing go directive must retain Go's module default")
	}
}

func TestModuleSnapshotAndManifestFailures(t *testing.T) {
	root := t.TempDir()
	manifest := filepath.Join(root, "go.mod")
	if err := os.WriteFile(manifest, []byte("module example.test/manifest\n\ngo 1.26.0\n"), 0600); err != nil {
		t.Fatal(err)
	}
	module := moduleIdentity{Path: "example.test/dep", GoMod: manifest, Replace: &moduleIdentity{Path: "../local", GoMod: manifest}}
	encoded, err := json.Marshal(module)
	if err != nil {
		t.Fatal(err)
	}
	modules, err := decodeModules(encoded)
	if err != nil || len(modules[module.Path].ModHash) != 64 || modules[module.Path].Replace.ModHash != modules[module.Path].ModHash {
		t.Fatal("dependency or local replacement manifest not bound", modules, err)
	}
	for _, data := range [][]byte{[]byte("invalid"), []byte(`{"Path":""}`), append(append([]byte{}, encoded...), encoded...)} {
		if _, err := decodeModules(data); err == nil {
			t.Fatalf("invalid module graph accepted: %q", data)
		}
	}
	module.GoMod = filepath.Join(root, "missing.mod")
	if err := hashModuleFile(&module); err == nil {
		t.Fatal("unreadable dependency manifest accepted")
	}
	if err := copyModuleSum(root, filepath.Join(root, "sum")); err == nil {
		t.Fatal("unreadable original sum treated as absent")
	}
	sum := filepath.Join(root, "original.sum")
	if err := os.WriteFile(sum, []byte("original checksum\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := copyModuleSum(sum, filepath.Join(root, "copied.sum")); err != nil {
		t.Fatal(err)
	}
	if err := copyModuleSum(sum, root); err == nil {
		t.Fatal("failed private sum write ignored")
	}
	if err := copyModuleSum(filepath.Join(root, "absent.sum"), root); err != nil {
		t.Fatal("legitimate missing original sum refused", err)
	}
	snapshot, err := snapshotModules("go", root, manifest, "readonly", filepath.Join(root, "snapshot.json"))
	if err != nil || !snapshot.Modules["example.test/manifest"].Main {
		t.Fatal("original module identity not recorded", snapshot, err)
	}
	if err := verifyModules(snapshot, "go", root, manifest, root); err != nil {
		t.Fatal("unchanged recipe refused", err)
	}
	if _, err := snapshotModules("go", root, manifest, "readonly", root); err == nil {
		t.Fatal("snapshot write failure ignored")
	}
	if err := verifyModules(snapshot, "/missing-go", root, manifest, root); err == nil {
		t.Fatal("module query failure ignored")
	}
	if _, err := readModuleSettings("/missing-go", root, manifest); err == nil {
		t.Fatal("module settings query failure ignored")
	}
}

func TestBuildRefusesExistingDependencyUpgrade(t *testing.T) {
	root := t.TempDir()
	for name, source := range map[string]string{
		"app/go.mod":  "module example.test/app\n\ngo 1.26.0\nrequire example.test/dep v1.0.0\nreplace example.test/dep v1.0.0 => ../v1\nreplace example.test/dep v1.1.0 => ../v2\n",
		"app/main.go": "package main\nimport(\"fmt\";\"example.test/dep\")\nfunc main(){fmt.Println(dep.Value)}\n",
		"v1/go.mod":   "module example.test/dep\n\ngo 1.26.0\n",
		"v1/dep.go":   "package dep\nconst Value=1\n",
		"v2/go.mod":   "module example.test/dep\n\ngo 1.26.0\n",
		"v2/dep.go":   "package dep\nconst Value=2\n",
		"sdk/go.mod":  "module io.quux.otelc/go\n\ngo 1.26.0\nrequire example.test/dep v1.1.0\n",
	} {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(source), 0600); err != nil {
			t.Fatal(err)
		}
	}
	app := filepath.Join(root, "app")
	baseline, err := command("go", app, "run", ".").CombinedOutput()
	if err != nil || string(baseline) != "1\n" {
		t.Fatal("baseline local dependency", string(baseline), err)
	}
	_, err = Build(testPlan(), ".", app, t.TempDir(), filepath.Join(root, "sdk"), "go")
	if err == nil || !strings.Contains(err.Error(), "existing Go dependency example.test/dep") {
		t.Fatal("MVS silently upgraded existing application dependency", err)
	}
}

func TestBuildRefusesLanguageChangeBeforeApplicationExecution(t *testing.T) {
	root := t.TempDir()
	manifest := []byte("module example.test/loop\n\ngo 1.20\n")
	source := []byte(`package main
import "fmt"
func main() {
 var callbacks []func() int
 for _, value := range []int{1,2,3} { callbacks = append(callbacks, func() int { return value }) }
 sum := 0
 for _, callback := range callbacks { sum += callback() }
 fmt.Println(sum)
}
`)
	if err := os.WriteFile(filepath.Join(root, "go.mod"), manifest, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "main.go"), source, 0600); err != nil {
		t.Fatal(err)
	}
	baseline, err := command("go", root, "run", ".").CombinedOutput()
	if err != nil || string(baseline) != "9\n" {
		t.Fatalf("original Go 1.20 semantics: %q, %v", baseline, err)
	}
	sdkRoot, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	p := testPlan()
	p.Functions.Include = []string{"never"}
	p.Metrics.Enabled = false
	binary, err := Build(p, ".", root, t.TempDir(), sdkRoot, "go")
	if err == nil || binary != "" || !strings.Contains(err.Error(), "Go language version") {
		t.Fatalf("SDK must not silently replace Go 1.20 loop semantics: binary=%q, error=%v", binary, err)
	}
	for name, want := range map[string][]byte{"go.mod": manifest, "main.go": source} {
		got, err := os.ReadFile(filepath.Join(root, name))
		if err != nil || !bytes.Equal(got, want) {
			t.Fatalf("original %s changed: %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "go.sum")); !os.IsNotExist(err) {
		t.Fatalf("original go.sum was created: %v", err)
	}
}
