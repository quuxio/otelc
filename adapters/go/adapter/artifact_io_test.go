//go:build unix

package adapter

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestArtifactFileBoundariesAndExclusiveWrites(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "input")
	data := []byte("original bytes\x00\xff")
	if err := writePrivate(file, data); err != nil {
		t.Fatal(err)
	}
	if err := writePrivate(file, []byte("replacement")); err == nil {
		t.Fatal("exclusive metadata write overwrote input")
	}
	actual, err := readBounded(file)
	if err != nil || !bytes.Equal(actual, data) {
		t.Fatal("metadata bytes changed", err)
	}
	if _, err := fileHash(filepath.Join(root, "absent")); err == nil {
		t.Fatal("missing input accepted")
	}
	if _, err := fileHash(root); err == nil {
		t.Fatal("directory accepted as input")
	}
	if _, err := readBounded(filepath.Join(root, "absent")); err == nil {
		t.Fatal("missing metadata accepted")
	}
	if _, err := readBounded(root); err == nil {
		t.Fatal("directory accepted as metadata")
	}
	link := filepath.Join(root, "link")
	if err := os.Symlink(file, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readBounded(link); err == nil {
		t.Fatal("metadata symlink accepted")
	}
	huge := filepath.Join(root, "huge")
	output, err := os.Create(huge)
	if err != nil {
		t.Fatal(err)
	}
	if err := output.Truncate(binaryLimit + 1); err != nil {
		t.Fatal(err)
	}
	if err := output.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := fileHash(huge); err == nil {
		t.Fatal("oversized binary accepted")
	}
	if _, err := readBounded(huge); err == nil {
		t.Fatal("oversized metadata accepted")
	}
	if err := writePrivate(filepath.Join(root, "oversized"), make([]byte, metadataLimit+1)); err == nil {
		t.Fatal("oversized metadata written")
	}
	if err := writePrivate(filepath.Join(root, "missing/record"), data); err == nil {
		t.Fatal("failed destination write ignored")
	}
	var record struct {
		Name string `json:"name"`
	}
	for _, value := range []string{"invalid", `{"unknown":1}`, `{"name":"one"} {"name":"two"}`} {
		if err := decodeStrict([]byte(value), &record); err == nil {
			t.Fatalf("invalid protocol accepted: %q", value)
		}
	}
	if err := decodeStrict([]byte(`{"name":"one"}`), &record); err != nil || record.Name != "one" {
		t.Fatal("valid protocol refused", err)
	}
	digest, err := fileHash(file)
	if err != nil {
		t.Fatal(err)
	}
	if err := copyExecutable(file, filepath.Join(root, "not-executable"), digest); err == nil {
		t.Fatal("non-executable file accepted")
	}
	if err := os.Chmod(file, 0500); err != nil {
		t.Fatal(err)
	}
	if err := copyExecutable(file, filepath.Join(root, "valid"), digest); err != nil {
		t.Fatal(err)
	}
	if err := copyExecutable(file, filepath.Join(root, "valid"), digest); err == nil {
		t.Fatal("existing executable overwritten")
	}
	if err := copyExecutable(file, filepath.Join(root, "wrong"), strings.Repeat("0", 64)); err == nil {
		t.Fatal("changed executable fingerprint accepted")
	}
	if err := copyExecutable(filepath.Join(root, "absent"), filepath.Join(root, "absent-copy"), digest); err == nil {
		t.Fatal("missing executable accepted")
	}
}

func TestRecordedCompilerFailuresAndInputChanges(t *testing.T) {
	root := t.TempDir()
	if err := recordedBuild("/missing-go", root, root, "missing", []string{"build"}); err == nil {
		t.Fatal("missing compiler accepted")
	}
	status, err := os.ReadFile(filepath.Join(root, "missing.status"))
	if err != nil || string(status) != "-1\n" {
		t.Fatal("failed-start compiler status lost", string(status), err)
	}
	if err := recordedBuild("go", root, root, "bad", []string{"unknown-command"}); err == nil {
		t.Fatal("compiler failure accepted")
	}
	if err := recordedBuild("go", root, root, "version", []string{"version"}); err != nil {
		t.Fatal(err)
	}
	stdout, err := os.ReadFile(filepath.Join(root, "version.stdout"))
	if err != nil || !bytes.HasPrefix(stdout, []byte("go version")) {
		t.Fatal("compiler stdout lost", string(stdout), err)
	}
	r := buildRecipe{Inputs: map[string]inputIdentity{}}
	input := filepath.Join(root, "source.go")
	if err := writePrivate(input, []byte("original")); err != nil {
		t.Fatal(err)
	}
	if err := r.input(input, false); err != nil {
		t.Fatal(err)
	}
	if err := r.input(input, false); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(input, []byte("changed"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := r.finish("unused"); err == nil || !strings.Contains(err.Error(), "input changed") {
		t.Fatal("input drift accepted", err)
	}
	r.Inputs = map[string]inputIdentity{}
	if err := r.input(filepath.Join(root, "missing.sum"), true); err != nil {
		t.Fatal(err)
	}
	r.Compiler.Tools = map[string]string{input: "wrong"}
	if err := r.finish("unused"); err == nil || !strings.Contains(err.Error(), "compiler tool changed") {
		t.Fatal("tool drift accepted", err)
	}
	r.Compiler.Tools = nil
	r.Compiler.Program = input
	r.Compiler.SHA256 = "wrong"
	if err := r.finish("unused"); err == nil || !strings.Contains(err.Error(), "driver changed") {
		t.Fatal("driver drift accepted", err)
	}
	if err := r.input(filepath.Join(root, "missing-required"), false); err == nil {
		t.Fatal("missing required source accepted")
	}
	if err := r.compiler("/missing-go", root); err == nil {
		t.Fatal("missing compiler identity accepted")
	}
}

func TestBundleBuildFailureLeavesNoLaunchableOutput(t *testing.T) {
	plan, data := artifactPlan(t, "http://127.0.0.1:1")
	root := t.TempDir()
	adapter, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{{"--run"}, {"--run-baseline"}, {"--build"}, {"--build", ".", "--wrong", "out"}, {"--build", "--flag", "--output-dir", "out"}} {
		if _, err := Run(plan, args, "/missing-sdk"); err == nil {
			t.Fatal("malformed build/run arguments accepted", args)
		}
	}
	output := filepath.Join(t.TempDir(), "bundle")
	if err := buildBundle(plan, ".", output, root, "/missing-sdk", "go", "/missing-adapter"); err == nil {
		t.Fatal("missing adapter was not refused")
	}
	if err := buildBundle("/missing-plan", ".", output, root, "/missing-sdk", "go", adapter); err == nil {
		t.Fatal("missing plan was not refused")
	}
	invalid := bytes.Replace(data, []byte(`"schema_version":2`), []byte(`"schema_version":1`), 1)
	if bytes.Equal(data, invalid) {
		t.Fatal("schema control did not alter the plan")
	}
	if err := os.WriteFile(plan, invalid, 0600); err != nil {
		t.Fatal(err)
	}
	if err := buildBundle(plan, ".", output, root, "/missing-sdk", "go", adapter); err == nil {
		t.Fatal("unresolved policy was not refused")
	}
	if err := os.WriteFile(plan, data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := buildBundle(plan, ".", output, root, "/missing-sdk", "/missing-go", adapter); err == nil {
		t.Fatal("missing compiler was not refused")
	}
	for name, data := range map[string]string{"go.mod": "module example.test/refused\n\ngo 1.26.0\n", "main.go": "package main\nfunc main(){}\n"} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	if err := buildBundle(plan, ".", output, root, "/missing-sdk", "go", adapter); err == nil {
		t.Fatal("missing SDK installation was not refused")
	}
	if _, err := os.Lstat(output); !os.IsNotExist(err) {
		t.Fatal("failed build left a launchable output", err)
	}
	if err := os.Symlink("/absent", output); err != nil {
		t.Fatal(err)
	}
	if err := buildBundle(plan, ".", output, root, "/missing-sdk", "/missing-go", adapter); err == nil {
		t.Fatal("dangling output symlink was followed")
	}
}
