package adapter

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"io.quux.otelc/go/policy"
)

type Package struct {
	Dir, ImportPath, Name string
	GoFiles, CgoFiles     []string
	Module                *struct{ Main bool }
}

func command(goTool, cwd string, args ...string) *exec.Cmd {
	cmd := exec.Command(goTool, args...)
	cmd.Dir = cwd
	cmd.Env = append(os.Environ(), "GOTOOLCHAIN=local")
	return cmd
}
func output(goTool, cwd string, args ...string) ([]byte, error) {
	cmd := command(goTool, cwd, args...)
	var errors bytes.Buffer
	cmd.Stderr = &errors
	data, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("go tool failed: %s", errors.String())
	}
	return data, nil
}
func Doctor(goTool, root string) error {
	data, err := output(goTool, root, "env", "GOVERSION")
	if err != nil {
		return err
	}
	version := strings.TrimPrefix(strings.TrimSpace(string(data)), "go1.")
	minor, err := strconv.Atoi(strings.Split(version, ".")[0])
	if err != nil || minor < 26 {
		return fmt.Errorf("go instrumentation requires Go 1.26+")
	}
	if policy.LegacyNilPanic(os.Getenv("GODEBUG")) {
		return fmt.Errorf("go panicnil=1 is unsupported")
	}
	flags := os.Getenv("GOFLAGS")
	for _, flag := range []string{"-overlay", "-modfile", "-toolexec", "-buildmode", "-mod=vendor"} {
		if strings.Contains(flags, flag) {
			return fmt.Errorf("go instrumentation cannot combine GOFLAGS %s", flag)
		}
	}
	workspace, err := output(goTool, root, "env", "GOWORK")
	if err != nil {
		return err
	}
	if value := strings.TrimSpace(string(workspace)); value != "" && value != "off" {
		return fmt.Errorf("go workspaces require separate adapter qualification")
	}
	return nil
}
func Build(plan policy.Plan, target, root, scratch, sdkRoot, goTool string) (string, error) {
	canonical, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", err
	}
	root = canonical
	if err := Doctor(goTool, root); err != nil {
		return "", err
	}
	module, err := output(goTool, root, "env", "GOMOD")
	if err != nil {
		return "", err
	}
	original := strings.TrimSpace(string(module))
	cwd := root
	modfile := filepath.Join(scratch, "go.mod")
	if original == "" || original == os.DevNull {
		if !strings.HasSuffix(target, ".go") {
			return "", fmt.Errorf("go package execution requires an existing go.mod")
		}
		target, err = filepath.Abs(target)
		if err != nil {
			return "", err
		}
		cwd = scratch
		err = os.WriteFile(modfile, []byte("module io.quux.otelc/generated\n\ngo 1.26.0\n"), 0600)
	} else {
		data, failure := os.ReadFile(original)
		if failure != nil {
			return "", failure
		}
		err = os.WriteFile(modfile, data, 0600)
		if sum, failure := os.ReadFile(filepath.Join(filepath.Dir(original), "go.sum")); failure == nil {
			err = os.WriteFile(filepath.Join(scratch, "go.sum"), sum, 0600)
		}
	}
	if err != nil {
		return "", err
	}
	if _, err = output(goTool, cwd, "mod", "edit", "-modfile="+modfile, "-require=io.quux.otelc/go@v0.0.0", "-replace=io.quux.otelc/go="+sdkRoot); err != nil {
		return "", err
	}
	// Resolution may update only our private alternate mod/sum, never originals.
	data, err := output(goTool, cwd, "list", "-deps", "-json", "-mod=mod", "-modfile="+modfile, target)
	if err != nil {
		return "", err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	packages := []Package{}
	for {
		var p Package
		if err = decoder.Decode(&p); err == io.EOF {
			break
		}
		if err != nil {
			return "", err
		}
		packages = append(packages, p)
	}
	replacements := map[string]string{}
	hasMain := false
	for _, p := range packages {
		relative, failure := filepath.Rel(root, p.Dir)
		if failure != nil || relative == ".." || strings.HasPrefix(relative, ".."+string(filepath.Separator)) {
			continue
		}
		if p.Module != nil && !p.Module.Main {
			continue
		}
		if len(p.CgoFiles) > 0 {
			return "", fmt.Errorf("selected Go project packages with cgo are not qualified")
		}
		for _, name := range p.GoFiles {
			filename := name
			if !filepath.IsAbs(filename) {
				filename = filepath.Join(p.Dir, name)
			}
			source, failure := os.ReadFile(filename)
			if failure != nil {
				return "", failure
			}
			rel, failure := filepath.Rel(root, filename)
			if failure != nil {
				return "", failure
			}
			identity := strings.ReplaceAll(strings.TrimSuffix(filepath.ToSlash(rel), ".go"), "/", ".")
			generated, _, failure := Transform(filename, source, identity, plan, plan.Sources.Accept(filepath.ToSlash(rel), false))
			if failure != nil {
				return "", failure
			}
			if p.Name == "main" && strings.Contains(string(generated), ".Close();") {
				hasMain = true
			}
			if bytes.Equal(source, generated) {
				continue
			}
			destination := filepath.Join(scratch, fmt.Sprintf("source-%d.go", len(replacements)))
			if failure = os.WriteFile(destination, generated, 0600); failure != nil {
				return "", failure
			}
			replacements[filename] = destination
		}
	}
	if !hasMain {
		return "", fmt.Errorf("go adapter requires an executable with a main function")
	}
	overlay := filepath.Join(scratch, "overlay.json")
	encoded, err := json.Marshal(map[string]any{"Replace": replacements})
	if err != nil {
		return "", err
	}
	if err = os.WriteFile(overlay, encoded, 0600); err != nil {
		return "", err
	}
	binary := filepath.Join(scratch, "application")
	if _, err = output(goTool, cwd, "build", "-mod=mod", "-modfile="+modfile, "-overlay="+overlay, "-o", binary, target); err != nil {
		return "", err
	}
	return binary, nil
}
func Run(planPath string, args []string, sdkRoot string) (int, error) {
	plan, err := policy.Load(planPath)
	if err != nil {
		return 1, err
	}
	root, err := os.Getwd()
	if err != nil {
		return 1, err
	}
	goTool := os.Getenv("OTELC_GO")
	if goTool == "" {
		goTool = "go"
	}
	if len(args) == 1 && args[0] == "--doctor" {
		if err = Doctor(goTool, root); err == nil {
			fmt.Println("Go: compiler overlays, deferred function probes and OTLP/HTTP metrics available")
		}
		return 0, err
	}
	if len(args) >= 2 && args[0] == "--inspect" {
		if len(args) > 3 || (len(args) == 3 && args[2] != "--json") {
			return 1, fmt.Errorf("go inspect requires SOURCE.go [--json]")
		}
		filename, err := filepath.Abs(args[1])
		if err != nil {
			return 1, err
		}
		data, err := os.ReadFile(filename)
		if err != nil {
			return 1, err
		}
		relative, err := filepath.Rel(root, filename)
		if err != nil {
			return 1, err
		}
		identity := strings.ReplaceAll(strings.TrimSuffix(filepath.ToSlash(relative), ".go"), "/", ".")
		_, functions, err := Transform(filename, data, identity, plan, plan.Sources.Accept(filepath.ToSlash(relative), false))
		if err != nil {
			return 1, err
		}
		return 0, json.NewEncoder(os.Stdout).Encode(map[string]any{"language": "go", "functions": functions})
	}
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return 1, fmt.Errorf("go requires SOURCE.go or PACKAGE [APP_ARGS...]")
	}
	scratch, err := os.MkdirTemp("", "otelc-go-")
	if err != nil {
		return 1, err
	}
	defer os.RemoveAll(scratch)
	binary, err := Build(plan, args[0], root, scratch, sdkRoot, goTool)
	if err != nil {
		return 1, err
	}
	cmd := exec.Command(binary, args[1:]...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Env = append(os.Environ(), "OTELC_GO_PLAN="+planPath)
	if err = cmd.Run(); err != nil {
		if failure, ok := err.(*exec.ExitError); ok {
			return failure.ExitCode(), nil
		}
		return 1, err
	}
	return 0, nil
}
