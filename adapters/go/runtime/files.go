package runtime

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"syscall"
)

const maxConfigBytes = 4 * 1024 * 1024

func privateDirectory(directory string) error {
	info, err := os.Lstat(directory)
	if err != nil {
		return err
	}
	identity, ok := info.Sys().(*syscall.Stat_t)
	if !filepath.IsAbs(directory) || !info.IsDir() || info.Mode().Perm() != 0700 || !ok || identity.Uid != uint32(os.Getuid()) {
		return errors.New("Go runtime requires an owned private 0700 directory")
	}
	return nil
}

// decodeObject requires exact case-sensitive fields and refuses duplicates,
// trailing objects and ambiguous nulls before applying the typed decoder.
func decodeObject(data []byte, keys []string, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	first, err := decoder.Token()
	if err != nil || first != json.Delim('{') {
		return errors.New("expected Go runtime JSON object")
	}
	allowed := make(map[string]bool, len(keys))
	for _, key := range keys {
		allowed[key] = true
	}
	seen := make(map[string]bool, len(keys))
	for decoder.More() {
		key, err := decoder.Token()
		if err != nil {
			return err
		}
		name, ok := key.(string)
		if !ok || !allowed[name] || seen[name] {
			return errors.New("unknown or duplicate Go runtime field")
		}
		seen[name] = true
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err != nil {
			return err
		}
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
			return errors.New("null Go runtime field")
		}
	}
	if _, err := decoder.Token(); err != nil {
		return err
	}
	var extra json.RawMessage
	if decoder.Decode(&extra) != io.EOF || len(seen) != len(allowed) {
		return errors.New("incomplete or trailing Go runtime object")
	}
	typed := json.NewDecoder(bytes.NewReader(data))
	typed.DisallowUnknownFields()
	return typed.Decode(value)
}

// Load reads the fixed private config and verifies its caller-bound content
// identity before starting an SDK, listener or application main function.
func Load(directory, expectedSHA256 string) (Config, error) {
	var config Config
	if !digestPattern.MatchString(expectedSHA256) {
		return config, errors.New("invalid Go configuration digest")
	}
	if err := privateDirectory(directory); err != nil {
		return config, err
	}
	file, err := os.OpenFile(filepath.Join(directory, "config.json"), os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return config, err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return config, err
	}
	identity, ok := info.Sys().(*syscall.Stat_t)
	if !info.Mode().IsRegular() || info.Mode().Perm() != 0600 || !ok || identity.Uid != uint32(os.Getuid()) || info.Size() > maxConfigBytes {
		return config, errors.New("expected bounded owned mode-0600 Go config")
	}
	data, err := io.ReadAll(io.LimitReader(file, maxConfigBytes+1))
	if err != nil {
		return config, err
	}
	digest := sha256.Sum256(data)
	if len(data) > maxConfigBytes || hex.EncodeToString(digest[:]) != expectedSHA256 {
		return config, errors.New("Go configuration content changed")
	}
	err = decodeObject(data, []string{"schema_version", "service_name", "endpoint", "interval_ms", "timeout_ms", "shutdown_timeout_ms",
		"collection", "emission", "traces_enabled", "sample_ratio", "functions", "lifetimes", "max_live", "boundaries", "policy_sha256", "selection_sha256"}, &config)
	if err != nil {
		return config, err
	}
	return config, config.validate()
}

func createJSON(directory, name string, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	file, err := os.OpenFile(filepath.Join(directory, name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(append(data, '\n'))
	closeErr := file.Close()
	return errors.Join(writeErr, closeErr)
}

func replaceHealth(directory string, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(directory, ".health-")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	_, writeErr := file.Write(append(data, '\n'))
	if err := errors.Join(writeErr, file.Close()); err != nil {
		return err
	}
	return os.Rename(name, filepath.Join(directory, "health.json"))
}
