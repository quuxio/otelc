package adapter

import (
	"bytes"
	"debug/buildinfo"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strconv"
)

type inputIdentity struct {
	SHA256 string `json:"sha256,omitempty"`
	Absent bool   `json:"absent,omitempty"`
}

type compilerIdentity struct {
	Program     string            `json:"program"`
	SHA256      string            `json:"sha256"`
	Version     string            `json:"version"`
	Environment map[string]string `json:"environment"`
	Tools       map[string]string `json:"tools"`
}

type buildRecipe struct {
	SchemaVersion    int                      `json:"schema_version"`
	Root             string                   `json:"source_root"`
	SDKRoot          string                   `json:"sdk_root"`
	WorkingDirectory string                   `json:"working_directory"`
	Target           string                   `json:"target"`
	Compiler         compilerIdentity         `json:"compiler"`
	Inputs           map[string]inputIdentity `json:"inputs"`
	BaselineArgs     []string                 `json:"baseline_argv"`
	InstrumentedArgs []string                 `json:"instrumented_argv"`
	Baseline         *debug.BuildInfo         `json:"baseline_build_info"`
	Instrumented     *debug.BuildInfo         `json:"instrumented_build_info"`
}

func recordedBuild(goTool, cwd, scratch, label string, args []string) error {
	cmd := command(goTool, cwd, args...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	failure := cmd.Run()
	status := -1
	if cmd.ProcessState != nil {
		status = cmd.ProcessState.ExitCode()
	}
	for name, data := range map[string][]byte{"stdout": stdout.Bytes(), "stderr": stderr.Bytes(), "status": []byte(strconv.Itoa(status) + "\n")} {
		if err := writePrivate(filepath.Join(scratch, label+"."+name), data); err != nil {
			return err
		}
	}
	if failure != nil {
		return fmt.Errorf("go %s failed: %w: %s", label, failure, stderr.String())
	}
	return nil
}

func (r *buildRecipe) input(path string, optional bool) error {
	if _, exists := r.Inputs[path]; exists {
		return nil
	}
	if len(r.Inputs) >= 65536 {
		return fmt.Errorf("go build input inventory exceeds 65536 files")
	}
	digest, err := fileHash(path)
	if optional && os.IsNotExist(err) {
		r.Inputs[path] = inputIdentity{Absent: true}
		return nil
	}
	if err != nil {
		return err
	}
	r.Inputs[path] = inputIdentity{SHA256: digest}
	return nil
}

func (r *buildRecipe) capturePackages(goTool, cwd, modfile, overlay, target string) error {
	mode := "readonly"
	if overlay != "" {
		mode = "mod"
	}
	args := []string{"list", "-deps", "-json", "-mod=" + mode, "-modfile=" + modfile}
	if overlay != "" {
		args = append(args, "-overlay="+overlay)
	}
	data, err := output(goTool, cwd, append(args, target)...)
	if err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	for {
		var p Package
		if err := decoder.Decode(&p); err == io.EOF {
			return nil
		} else if err != nil {
			return err
		}
		if p.Module != nil && p.Module.Main && len(p.SFiles)+len(p.SysoFiles) != 0 {
			return fmt.Errorf("go build bundles require separate qualification for project assembly/object inputs")
		}
		if p.Module != nil && !p.Module.Main && p.Module.GoMod != "" {
			if err := r.input(p.Module.GoMod, false); err != nil {
				return err
			}
		}
		groups := [][]string{p.GoFiles, p.CgoFiles, p.CFiles, p.CXXFiles, p.MFiles, p.HFiles, p.FFiles, p.SFiles, p.SysoFiles, p.EmbedFiles}
		for _, names := range groups {
			for _, name := range names {
				if !filepath.IsAbs(name) {
					name = filepath.Join(p.Dir, name)
				}
				if err := r.input(name, false); err != nil {
					return err
				}
			}
		}
	}
}

func (r *buildRecipe) compiler(goTool, cwd string) error {
	program, err := exec.LookPath(goTool)
	if err != nil {
		return err
	}
	program, err = filepath.EvalSymlinks(program)
	if err != nil {
		return err
	}
	r.Compiler.Program, err = filepath.Abs(program)
	if err != nil {
		return err
	}
	r.Compiler.SHA256, err = fileHash(r.Compiler.Program)
	if err != nil {
		return err
	}
	data, err := output(goTool, cwd, "env", "-json", "GOVERSION", "GOROOT", "GOTOOLDIR", "GOOS", "GOARCH", "GOAMD64", "GOARM64", "GOEXPERIMENT", "GOFLAGS", "CGO_ENABLED", "GOWORK")
	if err != nil {
		return err
	}
	if err := json.Unmarshal(data, &r.Compiler.Environment); err != nil {
		return err
	}
	environment := r.Compiler.Environment
	if environment["GOOS"] != runtime.GOOS || environment["GOARCH"] != runtime.GOARCH {
		return fmt.Errorf("go build bundles require the current host target")
	}
	r.Compiler.Version = environment["GOVERSION"]
	r.Compiler.Tools = map[string]string{}
	for _, name := range []string{"compile", "asm", "link"} {
		path := filepath.Join(environment["GOTOOLDIR"], name)
		digest, err := fileHash(path)
		if err != nil {
			return err
		}
		r.Compiler.Tools[path] = digest
	}
	return nil
}

func (r *buildRecipe) prepare(goTool, cwd, target, root, original, modfile, scratch, sdkRoot string) error {
	r.SchemaVersion = 1
	r.Root, r.WorkingDirectory, r.Target = root, cwd, target
	var err error
	r.SDKRoot, err = filepath.Abs(sdkRoot)
	if err != nil {
		return err
	}
	r.Inputs = map[string]inputIdentity{}
	if err := r.compiler(goTool, cwd); err != nil {
		return err
	}
	for _, module := range []string{original, filepath.Join(sdkRoot, "go.mod")} {
		if module == "" || module == os.DevNull {
			continue
		}
		if err := r.input(module, false); err != nil {
			return err
		}
		if err := r.input(filepath.Join(filepath.Dir(module), "go.sum"), true); err != nil {
			return err
		}
	}
	if err := r.input(filepath.Join(r.SDKRoot, "policy/policy.go"), false); err != nil {
		return err
	}
	baselineMod := filepath.Join(scratch, "baseline.mod")
	data, err := readBounded(modfile)
	if err != nil {
		return err
	}
	if err := os.WriteFile(baselineMod, data, 0600); err != nil {
		return err
	}
	if err := copyModuleSum(filepath.Join(scratch, "go.sum"), filepath.Join(scratch, "baseline.sum")); err != nil {
		return err
	}
	if err := r.capturePackages(goTool, cwd, baselineMod, "", target); err != nil {
		return err
	}
	r.BaselineArgs = []string{"build", "-mod=readonly", "-modfile=" + baselineMod, "-o", filepath.Join(scratch, "baseline"), target}
	if err := recordedBuild(goTool, cwd, scratch, "baseline-build", r.BaselineArgs); err != nil {
		return err
	}
	r.Baseline, err = buildinfo.ReadFile(filepath.Join(scratch, "baseline"))
	return err
}

func (r *buildRecipe) finish(binary string) error {
	for path, original := range r.Inputs {
		digest, err := fileHash(path)
		if original.Absent && os.IsNotExist(err) {
			continue
		}
		if err != nil || original.Absent || digest != original.SHA256 {
			return fmt.Errorf("go source/dependency input changed during the build: %s", path)
		}
	}
	for path, original := range r.Compiler.Tools {
		digest, err := fileHash(path)
		if err != nil || digest != original {
			return fmt.Errorf("go compiler tool changed during the build")
		}
	}
	digest, err := fileHash(r.Compiler.Program)
	if err != nil || digest != r.Compiler.SHA256 {
		return fmt.Errorf("go compiler driver changed during the build")
	}
	r.Instrumented, err = buildinfo.ReadFile(binary)
	if err != nil {
		return err
	}
	if r.Instrumented.GoVersion != r.Compiler.Version || r.Baseline.GoVersion != r.Compiler.Version {
		return fmt.Errorf("go compiler and embedded build versions differ")
	}
	return nil
}
