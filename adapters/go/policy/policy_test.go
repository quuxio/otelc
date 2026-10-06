package policy

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestResolvedPolicyAndByteSelection(t *testing.T) {
	for _, test := range []struct {
		pattern, name string
		want          bool
	}{{"(?-u).", "é", false}, {"(?-u)\\xc3\\xa9", "é", true}, {"(?-u).*", "a\nb", true}, {"[invalid", "abc", false}} {
		s := Selection{Include: []string{test.pattern}}
		if s.Accept(test.name, false) != test.want {
			t.Fatalf("byte selection %+v", test)
		}
	}
	s := Selection{Include: []string{".*"}, Exclude: []string{".*excluded.*"}}
	if s.Accept("excluded", true) || !s.Accept("included", false) {
		t.Fatal("exclusion precedence")
	}
	for _, value := range []string{"panicnil=1", "a=1,panicnil=1"} {
		if !LegacyNilPanic(value) {
			t.Fatal(value)
		}
	}
	for _, value := range []string{"", "panicnil=0", "panicnil=1,panicnil=0"} {
		if LegacyNilPanic(value) {
			t.Fatal(value)
		}
	}
	file := filepath.Join(t.TempDir(), "plan.json")
	if _, err := Load(file); err == nil {
		t.Fatal("missing plan accepted")
	}
	os.WriteFile(file, []byte("{"), 0600)
	if _, err := Load(file); err == nil {
		t.Fatal("invalid JSON accepted")
	}
	p := Plan{Language: "go", Available: true}
	p.Runtime.MaxFunctions = 2
	p.Runtime.MaxActive = 2
	p.Runtime.ShutdownMS = 100
	p.Export.IntervalMS = 100
	p.Export.TimeoutMS = 100
	data, _ := json.Marshal(p)
	os.WriteFile(file, data, 0600)
	if _, err := Load(file); err != nil {
		t.Fatal(err)
	}
	p.Language = "rust"
	data, _ = json.Marshal(p)
	os.WriteFile(file, data, 0600)
	if _, err := Load(file); err == nil {
		t.Fatal("wrong language accepted")
	}
	p.Language = "go"
	p.Runtime.MaxActive = 0
	data, _ = json.Marshal(p)
	os.WriteFile(file, data, 0600)
	if _, err := Load(file); err == nil {
		t.Fatal("invalid limit accepted")
	}
}
