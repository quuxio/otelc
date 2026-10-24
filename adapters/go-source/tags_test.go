package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTaggedRequestSelectsOriginalCompilerFiles(t *testing.T) {
	files := map[string]string{
		"main.go":    "package main\nfunc main(){Value()}\n",
		"default.go": "//go:build !feature || !π\n\npackage main\nfunc Value()int{return 11}\n",
		"feature.go": "//go:build feature && π\n\npackage main\nfunc Value()int{return 22}\n",
		"ignored.go": "//go:build absent\n\nthis is intentionally not Go source\n",
	}
	root := project(t, files)
	for _, tags := range [][]string{{"feature"}, {"feature", "π"}} {
		input, err := json.Marshal(map[string]any{"schema_version": 2, "root": root, "target": ".", "build_tags": tags})
		if err != nil {
			t.Fatal(err)
		}
		var output bytes.Buffer
		if err := run(bytes.NewReader(input), &output); err != nil {
			t.Fatal(err)
		}
		var got inventory
		if err := json.Unmarshal(output.Bytes(), &got); err != nil {
			t.Fatal(err)
		}
		var document map[string]json.RawMessage
		if err := json.Unmarshal(output.Bytes(), &document); err != nil {
			t.Fatal(err)
		}
		var recorded []string
		if err := json.Unmarshal(document["build_tags"], &recorded); err != nil {
			t.Fatal(err)
		}
		if got.SchemaVersion != 2 || !bytes.Equal(document["build_tags"], mustJSON(t, tags)) {
			t.Fatal("tagged inventory lost exact request", got.SchemaVersion, recorded)
		}
		wanted, excluded := "default.go", "feature.go"
		if len(tags) == 2 {
			wanted, excluded = excluded, wanted
		}
		if _, exists := got.CompilerInputs[wanted]; !exists {
			t.Fatal("active source missing", wanted)
		}
		for _, name := range []string{excluded, "ignored.go"} {
			if _, exists := got.CompilerInputs[name]; exists {
				t.Fatal("inactive source claimed compiled", name)
			}
		}
	}
	for name, expected := range files {
		actual, err := os.ReadFile(filepath.Join(root, name))
		if err != nil || string(actual) != expected {
			t.Fatal("original source changed", name, err)
		}
	}
}

func mustJSON(t *testing.T, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestTagsUseExactPinnedCompilerGrammarAndCanonicalSets(t *testing.T) {
	for _, tag := range []string{"feature", "9", "_", ".", "go1.27", "π", "١", "漢字"} {
		if err := validateTags([]string{tag}); err != nil {
			t.Fatal("valid compiler tag refused", tag, err)
		}
	}
	for _, tags := range [][]string{nil, {}, {"z", "a"}, {"a", "a"}, {""}, {" a"}, {"a "},
		{"a,b"}, {"a b"}, {"!a"}, {"a&&b"}, {"(a)"}, {"a\nb"}, {"a\x00"}, {"-gcflags=x"},
		{"a²"}, {"e\u0301"}, {strings.Repeat("a", 257)}, make([]string, 129)} {
		if validateTags(tags) == nil {
			t.Fatal("invalid compiler tag accepted", tags)
		}
		input := mustJSON(t, map[string]any{"schema_version": 2, "root": "/missing/source", "target": ".", "build_tags": tags})
		if err := run(bytes.NewReader(input), &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "tag") {
			t.Fatal("invalid tags reached root/compiler inspection", tags, err)
		}
	}
	if _, err := inspectTagged(taggedRequest{1, "/missing/source", ".", []string{"a"}}); err == nil {
		t.Fatal("tagged request accepted the legacy schema")
	}
}

func TestUntaggedRequestsKeepTheirClosedLegacyShape(t *testing.T) {
	root := project(t, map[string]string{"main.go": "package main\nfunc main(){}\n"})
	input := mustJSON(t, request{1, root, "."})
	var output bytes.Buffer
	if err := run(bytes.NewReader(input), &output); err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(output.Bytes(), []byte(`"build_tags"`)) || !bytes.Contains(output.Bytes(), []byte(`"schema_version":1`)) {
		t.Fatal("untagged inventory changed format", output.String())
	}
	for _, body := range []map[string]any{
		{"schema_version": 1, "root": root, "target": ".", "build_tags": []string{"feature"}},
		{"schema_version": 2, "root": root, "target": ".", "build_tags": []string{"feature"}, "build_flags": "-x"},
		{"schema_version": 2, "root": root, "target": ".", "build_tags": true},
	} {
		if err := run(bytes.NewReader(mustJSON(t, body)), &bytes.Buffer{}); err == nil {
			t.Fatal("invalid wire shape accepted", body)
		}
	}
}
