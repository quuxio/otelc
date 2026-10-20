package adapter

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"go/version"
	"io"
	"os"
	"path/filepath"
	"reflect"
)

type moduleIdentity struct {
	Path, Version, GoVersion, Sum, GoModSum string
	Dir, GoMod, ModHash                     string
	Main                                    bool
	Replace                                 *moduleIdentity
}

type moduleSettings struct {
	Go, Toolchain string
	Godebug       []struct{ Key, Value string }
}

type moduleSnapshot struct {
	Settings moduleSettings
	Modules  map[string]moduleIdentity
}

func copyModuleSum(source, destination string) error {
	data, err := os.ReadFile(source)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	return os.WriteFile(destination, data, 0600)
}

func readModuleSettings(goTool, cwd, modfile string) (moduleSettings, error) {
	var settings moduleSettings
	data, err := output(goTool, cwd, "mod", "edit", "-json", "-modfile="+modfile)
	if err != nil {
		return settings, err
	}
	err = json.Unmarshal(data, &settings)
	return settings, err
}

func decodeModules(data []byte) (map[string]moduleIdentity, error) {
	modules := map[string]moduleIdentity{}
	decoder := json.NewDecoder(bytes.NewReader(data))
	for {
		var module moduleIdentity
		if err := decoder.Decode(&module); err == io.EOF {
			return modules, nil
		} else if err != nil {
			return nil, err
		}
		if _, exists := modules[module.Path]; module.Path == "" || exists {
			return nil, fmt.Errorf("invalid or repeated Go module identity %q", module.Path)
		}
		if err := hashModuleFile(&module); err != nil {
			return nil, err
		}
		modules[module.Path] = module
	}
}

func hashModuleFile(module *moduleIdentity) error {
	// The private main go.mod necessarily gains the SDK requirement. Only
	// dependency manifests are immutable identities; main settings are separate.
	if !module.Main && module.GoMod != "" {
		data, err := os.ReadFile(module.GoMod)
		if err != nil {
			return err
		}
		digest := sha256.Sum256(data)
		module.ModHash = hex.EncodeToString(digest[:])
	}
	if module.Replace != nil {
		return hashModuleFile(module.Replace)
	}
	return nil
}

func snapshotModules(goTool, cwd, modfile, mode, record string) (moduleSnapshot, error) {
	var snapshot moduleSnapshot
	data, err := output(goTool, cwd, "list", "-m", "-json=Path,Version,Replace,Main,Dir,GoMod,GoVersion,Sum,GoModSum", "-mod="+mode, "-modfile="+modfile, "all")
	if err != nil {
		return snapshot, err
	}
	snapshot.Modules, err = decodeModules(data)
	if err != nil {
		return snapshot, err
	}
	snapshot.Settings, err = readModuleSettings(goTool, cwd, modfile)
	if err != nil {
		return snapshot, err
	}
	data, err = json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		return snapshot, err
	}
	err = os.WriteFile(record, append(data, '\n'), 0600)
	return snapshot, err
}

func languageVersion(goVersion string) string {
	if goVersion == "" {
		return "go1.16" // cmd/go's default for a module without a go directive.
	}
	return version.Lang("go" + goVersion)
}

func preserveModuleSettings(before, after moduleSettings) error {
	if languageVersion(before.Go) != languageVersion(after.Go) {
		return fmt.Errorf("instrumentation would change Go language version from %s to %s; this module requires separate SDK dependency isolation", languageVersion(before.Go), languageVersion(after.Go))
	}
	if before.Toolchain != after.Toolchain || !reflect.DeepEqual(before.Godebug, after.Godebug) {
		return fmt.Errorf("instrumentation would change the application's Go toolchain or GODEBUG defaults")
	}
	return nil
}

func sameModuleIdentity(before, after moduleIdentity) bool {
	if before.Path != after.Path || before.Version != after.Version || before.Main != after.Main {
		return false
	}
	if before.Main {
		return true // SDK requirements may change the private main manifest.
	}
	for _, pair := range [][2]string{{before.Sum, after.Sum}, {before.GoModSum, after.GoModSum}, {before.ModHash, after.ModHash}, {before.GoVersion, after.GoVersion}} {
		if pair[0] != "" && pair[0] != pair[1] {
			return false
		}
	}
	if (before.Replace == nil) != (after.Replace == nil) {
		return false
	}
	if before.Replace != nil {
		if before.Replace.Version == "" && before.Replace.Dir != after.Replace.Dir {
			return false
		}
		return sameModuleIdentity(*before.Replace, *after.Replace)
	}
	return true
}

func preserveModules(before, after moduleSnapshot) error {
	if err := preserveModuleSettings(before.Settings, after.Settings); err != nil {
		return err
	}
	for path, original := range before.Modules {
		resolved, exists := after.Modules[path]
		if !exists || !sameModuleIdentity(original, resolved) {
			return fmt.Errorf("instrumentation would change existing Go dependency %s (%s -> %s); this module requires separate SDK dependency isolation", path, original.Version, resolved.Version)
		}
	}
	return nil
}

func verifyModules(before moduleSnapshot, goTool, cwd, modfile, scratch string) error {
	after, err := snapshotModules(goTool, cwd, modfile, "mod", filepath.Join(scratch, "modules-instrumented.json"))
	if err != nil {
		return err
	}
	return preserveModules(before, after)
}
