package adapter

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"

	"io.quux.otelc/go/policy"
)

type bundleManifest struct {
	SchemaVersion int               `json:"schema_version"`
	Language      string            `json:"language"`
	Backend       string            `json:"backend"`
	AdapterSHA256 string            `json:"adapter_sha256"`
	Files         map[string]string `json:"files"`
}

func selectionIdentity(data []byte) (map[string]any, error) {
	p, err := policy.Decode(data)
	if err != nil {
		return nil, err
	}
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		return nil, err
	}
	if raw["schema_version"] != float64(2) || raw["backend"] != "compile" {
		return nil, fmt.Errorf("go bundles require the resolved schema-2 compile policy")
	}
	identity := map[string]any{"traces_enabled": p.Traces.Enabled}
	for _, key := range []string{"schema_version", "language", "backend", "sources", "functions", "source_matchers", "function_matchers", "annotations"} {
		identity[key] = raw[key]
	}
	return identity, nil
}

func buildBundle(planPath, target, directory, root, sdkRoot, goTool, adapter string) error {
	if _, err := os.Lstat(directory); !os.IsNotExist(err) {
		return fmt.Errorf("go artifact output directory must not already exist")
	}
	adapterHash, err := fileHash(adapter)
	if err != nil {
		return err
	}
	planBytes, err := readBounded(planPath)
	if err != nil {
		return err
	}
	if _, err := selectionIdentity(planBytes); err != nil {
		return err
	}
	plan, err := policy.Decode(planBytes)
	if err != nil {
		return err
	}
	scratch, err := os.MkdirTemp("", "otelc-go-build-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(scratch)
	recipe := buildRecipe{}
	if _, err := buildRecorded(plan, target, root, scratch, sdkRoot, goTool, &recipe); err != nil {
		return err
	}
	if err := validateRecipe(scratch, recipe); err != nil {
		return err
	}
	recipeBytes, err := json.MarshalIndent(recipe, "", "  ")
	if err != nil {
		return err
	}
	if err := writePrivate(filepath.Join(scratch, "plan.json"), planBytes); err != nil {
		return err
	}
	if err := writePrivate(filepath.Join(scratch, "recipe.json"), recipeBytes); err != nil {
		return err
	}
	manifest := bundleManifest{SchemaVersion: 1, Language: "go", Backend: "compile", Files: map[string]string{}}
	currentAdapterHash, err := fileHash(adapter)
	if err != nil || currentAdapterHash != adapterHash {
		return fmt.Errorf("go adapter changed during the build")
	}
	manifest.AdapterSHA256 = adapterHash
	if err := os.Mkdir(directory, 0700); err != nil {
		return err
	}
	complete := false
	defer func() {
		if !complete {
			os.RemoveAll(directory)
		}
	}()
	entries, err := os.ReadDir(scratch)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		name := entry.Name()
		input, destination := filepath.Join(scratch, name), filepath.Join(directory, name)
		digest, err := fileHash(input)
		if err != nil {
			return err
		}
		if name == "application" || name == "baseline" {
			err = copyExecutable(input, destination, digest)
		} else {
			var data []byte
			data, err = readBounded(input)
			if err == nil {
				err = writePrivate(destination, data)
			}
		}
		if err != nil {
			return err
		}
		manifest.Files[name] = digest
	}
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	// Publish the manifest last. An incomplete bundle cannot be replayed.
	if err := writePrivate(filepath.Join(directory, "build.json"), data); err != nil {
		return err
	}
	complete = true
	return nil
}

func validDigest(value string) bool {
	decoded, err := hex.DecodeString(value)
	return err == nil && len(decoded) == 32 && strings.ToLower(value) == value
}

func validateFiles(directory string, manifest bundleManifest) error {
	if manifest.SchemaVersion != 1 || manifest.Language != "go" || manifest.Backend != "compile" || !validDigest(manifest.AdapterSHA256) {
		return fmt.Errorf("unsupported Go artifact format")
	}
	for _, name := range []string{"application", "baseline", "plan.json", "recipe.json", "go.mod", "baseline.mod", "overlay.json", "modules-original.json", "modules-instrumented.json", "baseline-build.stdout", "baseline-build.stderr", "baseline-build.status", "instrumented-build.stdout", "instrumented-build.stderr", "instrumented-build.status"} {
		if !validDigest(manifest.Files[name]) {
			return fmt.Errorf("go artifact is missing required file %s", name)
		}
	}
	if len(manifest.Files) > 65536 {
		return fmt.Errorf("go bundle file inventory exceeds 65536 files")
	}
	for name, expected := range manifest.Files {
		if name == "." || filepath.Base(name) != name || strings.ContainsAny(name, "\\\x00") || len(name) > 200 || !validDigest(expected) {
			return fmt.Errorf("invalid Go artifact file identity")
		}
		path := filepath.Join(directory, name)
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() {
			return fmt.Errorf("go artifact files must be regular files")
		}
		if name != "application" && name != "baseline" && info.Size() > metadataLimit {
			return fmt.Errorf("go artifact metadata exceeds 4 MiB")
		}
		digest, err := fileHash(path)
		if err != nil || digest != expected {
			return fmt.Errorf("go artifact fingerprint mismatch for %s", name)
		}
	}
	return nil
}

func validateSelection(stored, current []byte) error {
	original, err := selectionIdentity(stored)
	if err != nil {
		return err
	}
	selected, err := selectionIdentity(current)
	if err != nil {
		return err
	}
	if !reflect.DeepEqual(original, selected) {
		return fmt.Errorf("go artifact selection, annotations or trace weaving changed; rebuild the artifact")
	}
	return nil
}
