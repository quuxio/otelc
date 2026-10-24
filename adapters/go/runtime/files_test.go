package runtime

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"go.opentelemetry.io/otel/trace"
)

func privateTemp(t *testing.T) string {
	t.Helper()
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	return directory
}

func configFile(t *testing.T, directory string, data []byte) string {
	t.Helper()
	if err := os.WriteFile(filepath.Join(directory, "config.json"), data, 0600); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}

func TestPrivateConfigExactDigestStrictFieldsAndPermissions(t *testing.T) {
	directory := privateTemp(t)
	cfg := configuration("http://127.0.0.1:4320")
	data, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	digest := configFile(t, directory, data)
	parsed, err := Load(directory, digest)
	if err != nil || parsed.PolicySHA256 != cfg.PolicySHA256 {
		t.Fatal("valid config refused", err)
	}
	if _, err := Load(directory, strings.Repeat("0", 64)); err == nil {
		t.Fatal("changed data accepted")
	}
	if _, err := Load(directory, "invalid"); err == nil {
		t.Fatal("invalid digest accepted")
	}
	if err := os.Chmod(filepath.Join(directory, "config.json"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(directory, digest); err == nil {
		t.Fatal("public config accepted")
	}
	if err := os.Chmod(filepath.Join(directory, "config.json"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(directory, 0755); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(directory, digest); err == nil {
		t.Fatal("public directory accepted")
	}
}

func TestConfigRefusesLinksFIFOSizeMissingAndMalformedObjects(t *testing.T) {
	directory := privateTemp(t)
	target := filepath.Join(directory, "actual.json")
	if err := os.WriteFile(target, []byte("{}"), 0600); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "config.json")
	if err := os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(directory, strings.Repeat("a", 64)); err == nil {
		t.Fatal("config link accepted")
	}
	os.Remove(path)
	if err := syscall.Mkfifo(path, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Load(directory, strings.Repeat("a", 64)); err == nil {
		t.Fatal("FIFO accepted")
	}
	os.Remove(path)
	large := []byte(strings.Repeat("x", maxConfigBytes+1))
	digest := configFile(t, directory, large)
	if _, err := Load(directory, digest); err == nil {
		t.Fatal("oversized config accepted")
	}
	os.Remove(path)
	if _, err := Load(directory, strings.Repeat("a", 64)); err == nil {
		t.Fatal("missing config accepted")
	}
	for _, raw := range []string{"[]", "{}", "null", "{", "{\"schema_version\":1,\"schema_version\":1}", "{\"unknown\":1}", "{\"schema_version\":null}", "{\"schema_version\":1}{}", "{\"schema_version\":\"1\"}"} {
		var value struct {
			Schema int `json:"schema_version"`
		}
		if decodeObject([]byte(raw), []string{"schema_version"}, &value) == nil {
			t.Fatalf("ambiguous JSON accepted: %q", raw)
		}
	}
	var value struct {
		Schema int `json:"schema_version"`
	}
	if err := decodeObject([]byte("{\"schema_version\":1}"), []string{"schema_version"}, &value); err != nil || value.Schema != 1 {
		t.Fatal("valid typed object refused")
	}
}

func TestOutputExclusivityAtomicHealthAndInvalidPaths(t *testing.T) {
	directory := privateTemp(t)
	if err := createJSON(directory, "health.json", map[string]string{"phase": "running"}); err != nil {
		t.Fatal(err)
	}
	if err := createJSON(directory, "health.json", map[string]string{"phase": "replacement"}); err == nil {
		t.Fatal("existing report overwritten")
	}
	if err := replaceHealth(directory, map[string]string{"phase": "final"}); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(filepath.Join(directory, "health.json"))
	if err != nil || !strings.Contains(string(data), "final") {
		t.Fatal("atomic report missing")
	}
	if err := replaceHealth(directory, make(chan int)); err == nil {
		t.Fatal("unencodable report accepted")
	}
	if err := createJSON(directory, "invalid.json", make(chan int)); err == nil {
		t.Fatal("unencodable initial report accepted")
	}
	if err := replaceHealth(filepath.Join(directory, "absent"), nil); err == nil {
		t.Fatal("missing runtime accepted")
	}
	if err := privateDirectory("."); err == nil {
		t.Fatal("relative directory accepted")
	}
	if err := privateDirectory(filepath.Join(directory, "absent")); err == nil {
		t.Fatal("missing directory accepted")
	}
	alias := filepath.Join(privateTemp(t), "linked")
	if err := os.Symlink(directory, alias); err != nil {
		t.Fatal(err)
	}
	if err := privateDirectory(alias); err == nil {
		t.Fatal("directory symlink accepted")
	}
	if _, err := New(configuration("http://127.0.0.1:1"), directory, func() trace.Span { return nil }, func(trace.Span) bool { return true }); err == nil {
		t.Fatal("existing runtime output accepted")
	}
}
