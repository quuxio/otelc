package adapter

import (
	"debug/buildinfo"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"

	"io.quux.otelc/go/policy"
)

func validateRecipe(directory string, recipe buildRecipe) error {
	if recipe.SchemaVersion != 1 || !filepath.IsAbs(recipe.Root) || !filepath.IsAbs(recipe.WorkingDirectory) || !filepath.IsAbs(recipe.Compiler.Program) || recipe.Target == "" || !validDigest(recipe.Compiler.SHA256) || len(recipe.Inputs) == 0 || len(recipe.Inputs) > 65536 || len(recipe.Compiler.Tools) != 3 {
		return fmt.Errorf("invalid Go compiler recipe")
	}
	if !filepath.IsAbs(recipe.SDKRoot) {
		return fmt.Errorf("invalid Go SDK identity")
	}
	for _, name := range []string{"go.mod", "go.sum", "policy/policy.go"} {
		identity, exists := recipe.Inputs[filepath.Join(recipe.SDKRoot, name)]
		if !exists || identity.Absent && name != "go.sum" {
			return fmt.Errorf("incomplete Go SDK module/lock/policy evidence")
		}
	}
	for path, identity := range recipe.Inputs {
		if !filepath.IsAbs(path) || (identity.Absent && identity.SHA256 != "") || (!identity.Absent && !validDigest(identity.SHA256)) {
			return fmt.Errorf("invalid Go source/dependency identity")
		}
	}
	for _, name := range []string{"compile", "asm", "link"} {
		if !validDigest(recipe.Compiler.Tools[filepath.Join(recipe.Compiler.Environment["GOTOOLDIR"], name)]) {
			return fmt.Errorf("invalid Go compiler tool identity")
		}
	}
	base, instrumented := recipe.BaselineArgs, recipe.InstrumentedArgs
	if len(base) != 6 || base[0] != "build" || base[1] != "-mod=readonly" || base[3] != "-o" || base[5] != recipe.Target || len(instrumented) != 7 || instrumented[0] != "build" || instrumented[1] != "-mod=mod" || instrumented[4] != "-o" || instrumented[6] != recipe.Target {
		return fmt.Errorf("incomplete Go baseline/instrumented compiler arguments")
	}
	if filepath.Base(strings.TrimPrefix(base[2], "-modfile=")) != "baseline.mod" || filepath.Base(base[4]) != "baseline" || filepath.Base(strings.TrimPrefix(instrumented[2], "-modfile=")) != "go.mod" || filepath.Base(strings.TrimPrefix(instrumented[3], "-overlay=")) != "overlay.json" || filepath.Base(instrumented[5]) != "application" {
		return fmt.Errorf("inconsistent Go compiler input/output recipe")
	}
	for _, name := range []string{"baseline-build.status", "instrumented-build.status"} {
		status, err := readBounded(filepath.Join(directory, name))
		if err != nil || string(status) != "0\n" {
			return fmt.Errorf("go artifact does not record two successful compiler builds")
		}
	}
	environment := recipe.Compiler.Environment
	if environment["GOOS"] != runtime.GOOS || environment["GOARCH"] != runtime.GOARCH {
		return fmt.Errorf("go artifact requires its original host target")
	}
	for _, field := range []string{"GOFLAGS", "GOOS", "GOARCH", "GOAMD64", "GOARM64", "GOEXPERIMENT", "CGO_ENABLED"} {
		if current, present := os.LookupEnv(field); present && current != environment[field] {
			return fmt.Errorf("go build setting %s changed; rebuild the artifact", field)
		}
	}
	if workspace := os.Getenv("GOWORK"); workspace != "" && workspace != "off" {
		return fmt.Errorf("go workspaces require separate adapter qualification")
	}
	for name, expected := range map[string]any{"baseline": recipe.Baseline, "application": recipe.Instrumented} {
		actual, err := buildinfo.ReadFile(filepath.Join(directory, name))
		if err != nil || !reflect.DeepEqual(actual, expected) || actual.GoVersion != recipe.Compiler.Version || actual.GoVersion != environment["GOVERSION"] {
			return fmt.Errorf("go artifact embedded build information mismatch for %s", name)
		}
		settings := map[string]string{}
		for _, setting := range actual.Settings {
			settings[setting.Key] = setting.Value
		}
		for _, field := range []string{"GOOS", "GOARCH", "GOAMD64", "GOARM64", "CGO_ENABLED"} {
			if environment[field] != "" && settings[field] != environment[field] {
				return fmt.Errorf("go artifact embedded target/flags mismatch")
			}
		}
	}
	var before, after moduleSnapshot
	for name, destination := range map[string]*moduleSnapshot{"modules-original.json": &before, "modules-instrumented.json": &after} {
		data, err := readBounded(filepath.Join(directory, name))
		if err != nil {
			return err
		}
		if err := decodeStrict(data, destination); err != nil {
			return err
		}
	}
	return validateModuleEvidence(before, after, recipe)
}

func validateModuleEvidence(before, after moduleSnapshot, recipe buildRecipe) error {
	for _, snapshot := range []moduleSnapshot{before, after} {
		mainCount := 0
		for path, module := range snapshot.Modules {
			if path == "" || module.Path != path {
				return fmt.Errorf("invalid Go module evidence: module path/key mismatch")
			}
			if module.Main {
				mainCount++
				if languageVersion(module.GoVersion) != languageVersion(snapshot.Settings.Go) {
					return fmt.Errorf("invalid Go module evidence: main language/settings mismatch")
				}
			}
		}
		if mainCount != 1 {
			return fmt.Errorf("incomplete Go module evidence: exactly one main module is required")
		}
	}
	sdk, exists := after.Modules["io.quux.otelc/go"]
	if !exists || sdk.Main || sdk.Replace == nil || sdk.Replace.Path != recipe.SDKRoot || sdk.Replace.Dir != recipe.SDKRoot || sdk.Replace.Version != "" {
		return fmt.Errorf("invalid Go module evidence: SDK replacement identity mismatch")
	}
	expected := recipe.Inputs[filepath.Join(recipe.SDKRoot, "go.mod")].SHA256
	if sdk.ModHash != expected || sdk.Replace.ModHash != expected {
		return fmt.Errorf("invalid Go module evidence: SDK manifest identity mismatch")
	}
	matched := 0
	for _, module := range recipe.Instrumented.Deps {
		if module.Path == sdk.Path && module.Version == sdk.Version && module.Replace != nil && module.Replace.Path == sdk.Replace.Path {
			matched++
		}
	}
	if matched != 1 {
		return fmt.Errorf("invalid Go module evidence: SDK embedded dependency mismatch")
	}
	return preserveModules(before, after)
}

func launchBundle(planPath, directory string, args []string, adapter string, baseline bool) (int, error) {
	data, err := readBounded(filepath.Join(directory, "build.json"))
	if err != nil {
		return 1, err
	}
	var manifest bundleManifest
	if err := decodeStrict(data, &manifest); err != nil {
		return 1, err
	}
	if err := validateFiles(directory, manifest); err != nil {
		return 1, err
	}
	digest, err := fileHash(adapter)
	if err != nil || digest != manifest.AdapterSHA256 {
		return 1, fmt.Errorf("go artifact requires its matching adapter; rebuild or use the original adapter")
	}
	stored, err := readBounded(filepath.Join(directory, "plan.json"))
	if err != nil {
		return 1, err
	}
	current, err := readBounded(planPath)
	if err != nil {
		return 1, err
	}
	if err := validateSelection(stored, current); err != nil {
		return 1, err
	}
	data, err = readBounded(filepath.Join(directory, "recipe.json"))
	if err != nil {
		return 1, err
	}
	var recipe buildRecipe
	if err := decodeStrict(data, &recipe); err != nil {
		return 1, err
	}
	if err := validateRecipe(directory, recipe); err != nil {
		return 1, err
	}
	if policy.LegacyNilPanic(os.Getenv("GODEBUG")) {
		return 1, fmt.Errorf("go panicnil=1 is unsupported")
	}
	scratch, err := os.MkdirTemp("", "otelc-go-replay-")
	if err != nil {
		return 1, err
	}
	defer os.RemoveAll(scratch)
	plan := filepath.Join(scratch, "plan.json")
	if err := writePrivate(plan, current); err != nil {
		return 1, err
	}
	name := "application"
	if baseline {
		name = "baseline"
	}
	binary := filepath.Join(scratch, "application")
	if err := copyExecutable(filepath.Join(directory, name), binary, manifest.Files[name]); err != nil {
		return 1, err
	}
	cmd := exec.Command(binary, args...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	cmd.Env = append(os.Environ(), "OTELC_GO_PLAN="+plan)
	if err := cmd.Run(); err != nil {
		if failure, ok := err.(*exec.ExitError); ok {
			return childExit(failure)
		}
		return 1, err
	}
	return 0, nil
}
