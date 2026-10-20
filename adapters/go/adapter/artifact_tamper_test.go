//go:build unix

package adapter

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGoBundleTamperingIsRefusedBeforeApplicationExecution(t *testing.T) {
	plan, _ := artifactPlan(t, "http://127.0.0.1:1")
	root := t.TempDir()
	for name, source := range map[string]string{
		"go.mod":  "module example.test/tamper\n\ngo 1.26.0\n",
		"main.go": "package main\nimport \"os\"\nfunc selected(){}\nfunc main(){os.WriteFile(os.Getenv(\"OTELC_ARTIFACT_SENTINEL\"),[]byte(\"ran\"),0600);selected()}\n",
	} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(source), 0600); err != nil {
			t.Fatal(err)
		}
	}
	sdk, err := filepath.Abs("..")
	if err != nil {
		t.Fatal(err)
	}
	adapter, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	bundle := filepath.Join(t.TempDir(), "bundle")
	if err := buildBundle(plan, ".", bundle, root, sdk, "go", adapter); err != nil {
		t.Fatal(err)
	}
	sentinel := filepath.Join(t.TempDir(), "ran")
	t.Setenv("OTELC_ARTIFACT_SENTINEL", sentinel)
	t.Setenv("OTELC_GO", "/missing-go")
	t.Setenv("PATH", "/missing-toolchain")
	manifestBytes, err := os.ReadFile(filepath.Join(bundle, "build.json"))
	if err != nil {
		t.Fatal(err)
	}
	recipeBytes, err := os.ReadFile(filepath.Join(bundle, "recipe.json"))
	if err != nil {
		t.Fatal(err)
	}
	checkRefused := func(t *testing.T, want string) {
		t.Helper()
		code, err := Run(plan, []string{"--run", bundle}, "/missing-sdk")
		if code != 1 || err == nil || !strings.Contains(err.Error(), want) {
			t.Fatalf("wanted %q refusal before launch: status=%d error=%v", want, code, err)
		}
		if _, err := os.Stat(sentinel); !os.IsNotExist(err) {
			t.Fatal("invalid artifact executed application", err)
		}
	}
	write := func(t *testing.T, name string, data []byte, mode os.FileMode) {
		t.Helper()
		path := filepath.Join(bundle, name)
		if err := os.Remove(path); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, data, mode); err != nil {
			t.Fatal(err)
		}
	}
	for _, test := range []struct {
		name, want string
		change     func(*bundleManifest)
	}{
		{"schema", "unsupported", func(m *bundleManifest) { m.SchemaVersion = 2 }},
		{"language", "unsupported", func(m *bundleManifest) { m.Language = "rust" }},
		{"adapter", "matching adapter", func(m *bundleManifest) { m.AdapterSHA256 = strings.Repeat("0", 64) }},
		{"required record", "missing required", func(m *bundleManifest) { delete(m.Files, "overlay.json") }},
		{"path traversal", "invalid Go artifact file", func(m *bundleManifest) { m.Files["../outside"] = strings.Repeat("0", 64) }},
	} {
		t.Run(test.name, func(t *testing.T) {
			var manifest bundleManifest
			if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
				t.Fatal(err)
			}
			test.change(&manifest)
			data, err := json.Marshal(manifest)
			if err != nil {
				t.Fatal(err)
			}
			write(t, "build.json", data, 0600)
			t.Cleanup(func() { write(t, "build.json", manifestBytes, 0600) })
			checkRefused(t, test.want)
		})
	}
	for _, test := range []struct {
		name, want string
		change     func(*buildRecipe)
	}{
		{"version", "build information mismatch", func(r *buildRecipe) { r.Compiler.Version = "go1.26.0" }},
		{"target", "original host target", func(r *buildRecipe) { r.Compiler.Environment["GOOS"] = "unqualified" }},
		{"embedded metadata", "build information mismatch", func(r *buildRecipe) { r.Baseline.Main.Path = "forged" }},
		{"source identity", "source/dependency identity", func(r *buildRecipe) { r.Inputs["/forged"] = inputIdentity{SHA256: "bad"} }},
		{"tools", "invalid Go compiler recipe", func(r *buildRecipe) { r.Compiler.Tools = nil }},
		{"compiler arguments", "compiler arguments", func(r *buildRecipe) { r.BaselineArgs = nil }},
		{"SDK root", "invalid Go SDK identity", func(r *buildRecipe) { r.SDKRoot = "relative" }},
		{"SDK policy", "incomplete Go SDK", func(r *buildRecipe) { delete(r.Inputs, filepath.Join(r.SDKRoot, "policy/policy.go")) }},
		{"SDK lock", "incomplete Go SDK", func(r *buildRecipe) { delete(r.Inputs, filepath.Join(r.SDKRoot, "go.sum")) }},
	} {
		t.Run("rehashed "+test.name, func(t *testing.T) {
			var recipe buildRecipe
			if err := json.Unmarshal(recipeBytes, &recipe); err != nil {
				t.Fatal(err)
			}
			test.change(&recipe)
			data, err := json.Marshal(recipe)
			if err != nil {
				t.Fatal(err)
			}
			write(t, "recipe.json", data, 0600)
			var manifest bundleManifest
			if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
				t.Fatal(err)
			}
			manifest.Files["recipe.json"], err = fileHash(filepath.Join(bundle, "recipe.json"))
			if err != nil {
				t.Fatal(err)
			}
			data, err = json.Marshal(manifest)
			if err != nil {
				t.Fatal(err)
			}
			write(t, "build.json", data, 0600)
			t.Cleanup(func() { write(t, "recipe.json", recipeBytes, 0600); write(t, "build.json", manifestBytes, 0600) })
			checkRefused(t, test.want)
		})
	}
	for _, name := range []string{"application", "overlay.json"} {
		t.Run(name+" bytes", func(t *testing.T) {
			path := filepath.Join(bundle, name)
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			mode := os.FileMode(0600)
			if name == "application" {
				mode = 0500
			}
			write(t, name, append(append([]byte{}, data...), 0), mode)
			t.Cleanup(func() { write(t, name, data, mode) })
			checkRefused(t, "fingerprint mismatch")
		})
	}
	beforeBytes, err := os.ReadFile(filepath.Join(bundle, "modules-original.json"))
	if err != nil {
		t.Fatal(err)
	}
	afterBytes, err := os.ReadFile(filepath.Join(bundle, "modules-instrumented.json"))
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name   string
		change func(*moduleSnapshot, *moduleSnapshot)
	}{
		{"empty graphs", func(before, after *moduleSnapshot) { before.Modules = nil; after.Modules = nil }},
		{"missing original main", func(before, after *moduleSnapshot) {
			for key, value := range before.Modules {
				if value.Main {
					delete(before.Modules, key)
				}
			}
		}},
		{"duplicate main", func(before, after *moduleSnapshot) {
			for _, value := range before.Modules {
				if value.Main {
					value.Path = "forged/second-main"
					before.Modules[value.Path] = value
					break
				}
			}
		}},
		{"key mismatch", func(before, after *moduleSnapshot) {
			for key, value := range before.Modules {
				if value.Main {
					value.Path = "forged/main"
					before.Modules[key] = value
					after.Modules[key] = value
				}
			}
		}},
		{"language mismatch", func(before, after *moduleSnapshot) { before.Settings.Go = "1.27.2"; after.Settings.Go = "1.27.2" }},
		{"missing SDK", func(before, after *moduleSnapshot) { delete(after.Modules, "io.quux.otelc/go") }},
		{"SDK manifest", func(before, after *moduleSnapshot) {
			sdk := after.Modules["io.quux.otelc/go"]
			sdk.ModHash = strings.Repeat("0", 64)
			sdk.Replace.ModHash = sdk.ModHash
			after.Modules[sdk.Path] = sdk
		}},
		{"SDK version", func(before, after *moduleSnapshot) {
			sdk := after.Modules["io.quux.otelc/go"]
			sdk.Version = "v999.0.0"
			after.Modules[sdk.Path] = sdk
		}},
	} {
		t.Run("module evidence "+test.name, func(t *testing.T) {
			var before, after moduleSnapshot
			if err := json.Unmarshal(beforeBytes, &before); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(afterBytes, &after); err != nil {
				t.Fatal(err)
			}
			test.change(&before, &after)
			var manifest bundleManifest
			if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
				t.Fatal(err)
			}
			for name, snapshot := range map[string]moduleSnapshot{"modules-original.json": before, "modules-instrumented.json": after} {
				data, err := json.Marshal(snapshot)
				if err != nil {
					t.Fatal(err)
				}
				write(t, name, data, 0600)
				manifest.Files[name], err = fileHash(filepath.Join(bundle, name))
				if err != nil {
					t.Fatal(err)
				}
			}
			data, err := json.Marshal(manifest)
			if err != nil {
				t.Fatal(err)
			}
			write(t, "build.json", data, 0600)
			t.Cleanup(func() {
				write(t, "modules-original.json", beforeBytes, 0600)
				write(t, "modules-instrumented.json", afterBytes, 0600)
				write(t, "build.json", manifestBytes, 0600)
				os.Remove(sentinel)
			})
			checkRefused(t, "module evidence")
		})
	}
	t.Run("metadata symlink", func(t *testing.T) {
		data, err := os.ReadFile(filepath.Join(bundle, "plan.json"))
		if err != nil {
			t.Fatal(err)
		}
		outside := filepath.Join(t.TempDir(), "outside")
		if err := os.WriteFile(outside, data, 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Remove(filepath.Join(bundle, "plan.json")); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(outside, filepath.Join(bundle, "plan.json")); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { write(t, "plan.json", data, 0600) })
		checkRefused(t, "regular files")
	})
	t.Run("trailing document", func(t *testing.T) {
		write(t, "build.json", append(append([]byte{}, manifestBytes...), []byte("{}")...), 0600)
		t.Cleanup(func() { write(t, "build.json", manifestBytes, 0600) })
		checkRefused(t, "one JSON document")
	})
	originalPlan, err := os.ReadFile(plan)
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name   string
		change func(map[string]any)
	}{
		{"functions", func(p map[string]any) { p["function_matchers"].(map[string]any)["include"] = []string{"never"} }},
		{"sources", func(p map[string]any) { p["source_matchers"].(map[string]any)["include"] = []string{"never"} }},
		{"annotations", func(p map[string]any) { p["annotations"].(map[string]any)["read_existing"] = false }},
		{"trace weaving", func(p map[string]any) { p["traces"].(map[string]any)["enabled"] = false }},
	} {
		t.Run("current "+test.name, func(t *testing.T) {
			var raw map[string]any
			if err := json.Unmarshal(originalPlan, &raw); err != nil {
				t.Fatal(err)
			}
			test.change(raw)
			data, err := json.Marshal(raw)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(plan, data, 0600); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() {
				if err := os.WriteFile(plan, originalPlan, 0600); err != nil {
					t.Error(err)
				}
			})
			checkRefused(t, "rebuild")
		})
	}
	for _, test := range []struct{ key, value, want string }{
		{"GOFLAGS", "-tags=otelc_rebuild_required", "build setting"},
		{"GOWORK", "/missing-workspace", "workspaces require"},
		{"GODEBUG", "panicnil=1", "panicnil=1"},
	} {
		t.Run("environment "+test.key, func(t *testing.T) { t.Setenv(test.key, test.value); checkRefused(t, test.want) })
	}
}
