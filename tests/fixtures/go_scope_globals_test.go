// Copyright quuxio
// SPDX-License-Identifier: Apache-2.0

package instrument

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"go.opentelemetry.io/otelc/tool/internal/ast"
)

func TestQuuxRawOnlyGlobals(t *testing.T) {
	ip := newTestPhase()
	ip.workDir = t.TempDir()
	if err := ip.writeGlobals("application"); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(ip.workDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 0 || len(ip.compileArgs) != 0 {
		t.Fatal("raw-only instrumentation injected an unused HookContext declaration")
	}
}

func TestQuuxTrampolineGlobalsRemain(t *testing.T) {
	ip := newTestPhase()
	ip.workDir = t.TempDir()
	root, err := ast.NewAstParser().ParseSource("package application\nvar quuxHookFlag bool\n")
	if err != nil {
		t.Fatal(err)
	}
	ip.varDecls = root.Decls
	if err := ip.writeGlobals("application"); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(ip.workDir, otelcGlobalsFile)
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(ip.compileArgs) != 1 || ip.compileArgs[0] != path {
		t.Fatal("required trampoline globals were not passed to the compiler")
	}
	if !strings.Contains(string(raw), "quuxHookFlag") || !strings.Contains(string(raw), "type HookContext interface") {
		t.Fatal("required trampoline declarations or HookContext API were lost")
	}
}
