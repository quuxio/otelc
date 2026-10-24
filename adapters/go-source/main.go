// The source inspector reads compiler-selected Go packages without loading or
// executing application code. Its output supplies facts, never policy choices.
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"go/ast"
	"go/build/constraint"
	"go/parser"
	"go/token"
	"go/types"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"golang.org/x/tools/go/packages"
)

type request struct {
	SchemaVersion int    `json:"schema_version"`
	Root          string `json:"root"`
	Target        string `json:"target"`
}

type taggedRequest struct {
	SchemaVersion int      `json:"schema_version"`
	Root          string   `json:"root"`
	Target        string   `json:"target"`
	BuildTags     []string `json:"build_tags"`
}

type location struct {
	File   string `json:"file"`
	Line   int    `json:"line"`
	SHA256 string `json:"sha256"`
}

type result struct {
	Expression string `json:"expression"`
	Error      bool   `json:"error"`
	Failure    string `json:"failure"`
	Pointer    string `json:"pointer"`
}

type function struct {
	location
	Name       string   `json:"name"`
	Package    string   `json:"package"`
	Symbol     string   `json:"symbol"`
	Receiver   string   `json:"receiver"`
	ReceiverID string   `json:"receiver_id"`
	Generic    bool     `json:"generic"`
	Parameters int      `json:"parameters"`
	Variadic   bool     `json:"variadic"`
	Results    []result `json:"results"`
	Gap        string   `json:"gap"`
}

type objectType struct {
	location
	Name    string `json:"name"`
	Package string `json:"package"`
	Symbol  string `json:"symbol"`
	Size    int64  `json:"size"`
	Gap     string `json:"gap"`
}

type allocation struct {
	location
	Type  string `json:"type"`
	Start int    `json:"start"`
	End   int    `json:"end"`
	Kind  string `json:"kind"`
	Gap   string `json:"gap"`
}

type inventory struct {
	SchemaVersion  int                    `json:"schema_version"`
	Root           string                 `json:"root"`
	Target         string                 `json:"target"`
	Main           string                 `json:"main"`
	Functions      []function             `json:"functions"`
	Types          []objectType           `json:"types"`
	Allocations    []allocation           `json:"allocations"`
	Files          map[string]string      `json:"files"`
	Packages       map[string]packageFact `json:"packages"`
	CompilerInputs map[string]string      `json:"compiler_inputs"`
	BuildTags      []string               `json:"build_tags,omitempty"`
}

type packageFact struct {
	Name      string `json:"name"`
	Directory string `json:"directory"`
}

func validate(r request) error {
	clean := filepath.ToSlash(filepath.Clean(r.Target))
	if r.SchemaVersion != 1 || !filepath.IsAbs(r.Root) || filepath.Clean(r.Root) != r.Root ||
		(r.Target != "." && !strings.HasPrefix(r.Target, "./")) || strings.Contains(r.Target, "...") ||
		strings.HasPrefix(clean, "../") || clean == ".." || filepath.IsAbs(r.Target) ||
		(r.Target != "." && "./"+clean != r.Target) {
		return errors.New("expected schema1, canonical absolute root and one relative package target")
	}
	resolved, err := filepath.EvalSymlinks(r.Root)
	if err != nil || resolved != r.Root {
		return errors.New("Go source root must exist without symlink aliases")
	}
	return nil
}

func validateTags(tags []string) error {
	if len(tags) == 0 || len(tags) > 128 || !sort.StringsAreSorted(tags) {
		return errors.New("tagged inspection requires a canonical nonempty tag set")
	}
	for index, tag := range tags {
		if len(tag) > 256 || (index > 0 && tags[index-1] == tag) {
			return errors.New("Go build tag is oversized or repeated")
		}
		expression, err := constraint.Parse("//go:build " + tag)
		single, ok := expression.(*constraint.TagExpr)
		if err != nil || !ok || single.Tag != tag {
			return errors.New("Go build tag must be one exact compiler tag, not an expression")
		}
	}
	return nil
}

func digest(data []byte) string {
	value := sha256.Sum256(data)
	return hex.EncodeToString(value[:])
}

func inside(root, path string) (string, error) {
	name, err := filepath.Rel(root, path)
	resolved, linkErr := filepath.EvalSymlinks(path)
	if err != nil || linkErr != nil || resolved != path || name == ".." || strings.HasPrefix(name, ".."+string(filepath.Separator)) {
		return "", fmt.Errorf("source path is outside the original module or has symlink aliases: %s", path)
	}
	return filepath.ToSlash(name), nil
}

func named(t types.Type) *types.Named {
	value, _ := types.Unalias(t).(*types.Named)
	return value
}

func pointer(t types.Type, owner *types.Package) string {
	p, ok := types.Unalias(t).(*types.Pointer)
	if !ok {
		return ""
	}
	n := named(p.Elem())
	if n == nil || n.Obj().Pkg() == nil || owner != nil && n.Obj().Pkg() != owner || n.TypeArgs().Len() != 0 || n.TypeParams().Len() != 0 {
		return ""
	}
	return n.Obj().Pkg().Path() + "." + n.Obj().Name()
}

func allocations(p *packages.Package, file *ast.File, at location) []allocation {
	rows := []allocation{}
	ast.Inspect(file, func(node ast.Node) bool {
		expr, ok := node.(ast.Expr)
		if !ok {
			return true
		}
		kind, gap := "", ""
		switch e := expr.(type) {
		case *ast.UnaryExpr:
			if e.Op == token.AND {
				kind = "address"
				if _, ok := e.X.(*ast.CompositeLit); ok {
					kind = "composite"
				} else {
					gap = "address of existing storage needs a construction/storage binding"
				}
			}
		case *ast.CallExpr:
			if id, ok := e.Fun.(*ast.Ident); ok {
				if builtin, ok := p.TypesInfo.Uses[id].(*types.Builtin); ok && builtin.Name() == "new" {
					kind = "new"
				}
			}
		}
		if kind == "" {
			return true
		}
		name := pointer(p.TypesInfo.TypeOf(expr), nil)
		if name == "" {
			return true
		}
		start, end := p.Fset.Position(expr.Pos()), p.Fset.Position(expr.End())
		row := allocation{location: at, Type: name, Start: start.Offset, End: end.Offset, Kind: kind, Gap: gap}
		row.Line = start.Line
		rows = append(rows, row)
		return true
	})
	return rows
}

func functionFact(p *packages.Package, decl *ast.FuncDecl, at location) function {
	obj := p.TypesInfo.Defs[decl.Name].(*types.Func)
	sig := obj.Type().(*types.Signature)
	f := function{location: at, Package: p.PkgPath, Symbol: decl.Name.Name,
		Name: p.PkgPath + "." + decl.Name.Name, Generic: sig.TypeParams().Len() != 0 || sig.RecvTypeParams().Len() != 0,
		Parameters: sig.Params().Len(), Variadic: sig.Variadic(), Results: []result{}}
	if sig.Recv() != nil {
		f.Receiver = sig.Recv().Name()
		t := sig.Recv().Type()
		prefix := ""
		if ptr, ok := t.(*types.Pointer); ok {
			t, prefix = ptr.Elem(), "*"
		}
		n := named(t)
		f.ReceiverID = prefix + n.Obj().Name()
		f.Name = p.PkgPath + "." + n.Obj().Name() + "." + f.Symbol
		if n.TypeParams().Len() != 0 {
			f.Generic = true
			f.Gap = "generic receivers require a separate upstream rule binding"
		}
	}
	errorType := types.Universe.Lookup("error").Type()
	for index := 0; index < sig.Results().Len(); index++ {
		v := sig.Results().At(index)
		expression := v.Name()
		if expression == "" {
			expression = fmt.Sprintf("_unnamedRetVal%d", index)
		}
		value := result{Expression: expression, Error: types.AssignableTo(v.Type(), errorType), Pointer: pointer(v.Type(), p.Types)}
		if value.Error {
			value.Failure = "true"
			switch v.Type().Underlying().(type) {
			case *types.Pointer, *types.Interface, *types.Slice, *types.Map, *types.Chan, *types.Signature:
				value.Failure = expression + " != nil"
			}
			if _, ok := v.Type().(*types.TypeParam); ok {
				f.Gap = "generic error result requires a concrete nilability binding"
			}
		}
		f.Results = append(f.Results, value)
		if expression == "_" {
			f.Gap = "blank result names require a separate injection profile"
		}
	}
	if f.Symbol == "init" || decl.Body == nil {
		f.Gap = "init or bodyless functions do not have a supported invocation binding"
	}
	return f
}

func typeFact(p *packages.Package, spec *ast.TypeSpec, at location) objectType {
	f := objectType{location: at, Name: p.PkgPath + "." + spec.Name.Name, Package: p.PkgPath, Symbol: spec.Name.Name, Size: -1}
	obj := p.TypesInfo.Defs[spec.Name].(*types.TypeName)
	n := named(obj.Type())
	if obj.IsAlias() || n == nil || n.TypeParams().Len() != 0 {
		f.Gap = "aliases and generic type declarations are not distinct concrete lifetime bindings"
		return f
	}
	f.Size = p.TypesSizes.Sizeof(n)
	if f.Size < 16 {
		f.Gap = "objects below16bytes lack an independent GC observation in this profile"
	}
	return f
}

func scanFile(r request, p *packages.Package, file *ast.File, parsed map[string]string, out *inventory) error {
	absolute := p.Fset.Position(file.Pos()).Filename
	name, err := inside(r.Root, absolute)
	if err != nil {
		return err
	}
	hash, ok := parsed[absolute]
	if !ok {
		return errors.New("Go source lacks the exact parser input identity")
	}
	out.Files[name] = hash
	out.Allocations = append(out.Allocations, allocations(p, file, location{File: name, SHA256: hash})...)
	for _, decl := range file.Decls {
		at := location{File: name, Line: p.Fset.Position(decl.Pos()).Line, SHA256: hash}
		switch item := decl.(type) {
		case *ast.FuncDecl:
			out.Functions = append(out.Functions, functionFact(p, item, at))
		case *ast.GenDecl:
			if item.Tok == token.TYPE {
				for _, s := range item.Specs {
					spec := s.(*ast.TypeSpec)
					at.Line = p.Fset.Position(spec.Pos()).Line
					out.Types = append(out.Types, typeFact(p, spec, at))
				}
			}
		}
	}
	return nil
}

func inspect(r request) (inventory, error) {
	return inspectWithTags(r, nil)
}

func inspectTagged(r taggedRequest) (inventory, error) {
	if r.SchemaVersion != 2 {
		return inventory{}, errors.New("tagged Go inspection requires schema2")
	}
	if err := validateTags(r.BuildTags); err != nil {
		return inventory{}, err
	}
	return inspectWithTags(request{1, r.Root, r.Target}, r.BuildTags)
}

func inspectWithTags(r request, tags []string) (inventory, error) {
	out := inventory{SchemaVersion: 1, Root: r.Root, Target: r.Target, Functions: []function{}, Types: []objectType{}, Allocations: []allocation{}, Files: map[string]string{}, Packages: map[string]packageFact{}, CompilerInputs: map[string]string{}}
	if err := validate(r); err != nil {
		return out, err
	}
	flags := []string{"-mod=readonly"}
	if tags != nil {
		out.SchemaVersion = 2
		out.BuildTags = append([]string(nil), tags...)
		flags = append(flags, "-tags="+strings.Join(tags, ","))
	}
	if driver := os.Getenv("GOPACKAGESDRIVER"); driver != "" && driver != "off" {
		return out, errors.New("external Go package drivers are outside this source profile")
	}
	mode := packages.NeedName | packages.NeedFiles | packages.NeedCompiledGoFiles | packages.NeedImports |
		packages.NeedDeps | packages.NeedTypes | packages.NeedSyntax | packages.NeedTypesInfo | packages.NeedModule | packages.NeedTypesSizes | packages.NeedEmbedFiles
	parsed := map[string]string{}
	var mu sync.Mutex
	parse := func(fset *token.FileSet, filename string, src []byte) (*ast.File, error) {
		if src == nil {
			var err error
			src, err = os.ReadFile(filename)
			if err != nil {
				return nil, err
			}
		}
		mu.Lock()
		parsed[filename] = digest(src)
		mu.Unlock()
		return parser.ParseFile(fset, filename, src, parser.ParseComments)
	}
	loaded, err := packages.Load(&packages.Config{Dir: r.Root, Mode: mode, BuildFlags: flags, ParseFile: parse}, r.Target)
	if err != nil {
		return out, err
	}
	if len(loaded) != 1 || loaded[0].Name != "main" {
		return out, errors.New("Go build target must resolve exactly one main package")
	}
	out.Main = loaded[0].PkgPath
	packages.Visit(loaded, func(p *packages.Package) bool {
		if len(p.Errors) != 0 {
			err = fmt.Errorf("Go compiler package %s: %s", p.PkgPath, p.Errors[0].Msg)
		}
		return true
	}, func(p *packages.Package) {
		if err != nil || p.Module == nil || !p.Module.Main {
			return
		}
		if p.Module.Dir != r.Root {
			err = errors.New("Go workspace modules require a separate source profile")
			return
		}
		for _, file := range p.Syntax {
			if err = scanFile(r, p, file, parsed, &out); err != nil {
				return
			}
			directory := filepath.Dir(p.Fset.Position(file.Pos()).Filename)
			relative, relErr := filepath.Rel(r.Root, directory)
			if relErr != nil {
				err = relErr
				return
			}
			out.Packages[p.PkgPath] = packageFact{Name: p.Name, Directory: filepath.ToSlash(relative)}
		}
		for _, absolute := range append(append(append([]string{}, p.GoFiles...), p.OtherFiles...), p.EmbedFiles...) {
			name, pathErr := inside(r.Root, absolute)
			if pathErr != nil {
				err = pathErr
				return
			}
			data, readErr := os.ReadFile(absolute)
			if readErr != nil {
				err = readErr
				return
			}
			out.CompilerInputs[name] = digest(data)
		}
	})
	if err != nil {
		return out, err
	}
	for name, before := range out.Files {
		data, readErr := os.ReadFile(filepath.Join(r.Root, filepath.FromSlash(name)))
		if readErr != nil || digest(data) != before {
			return out, errors.New("Go source changed during inspection")
		}
	}
	for name, before := range out.CompilerInputs {
		data, readErr := os.ReadFile(filepath.Join(r.Root, filepath.FromSlash(name)))
		if readErr != nil || digest(data) != before {
			return out, errors.New("Go compiler input changed during inspection")
		}
	}
	sort.Slice(out.Functions, func(i, j int) bool { return out.Functions[i].Name < out.Functions[j].Name })
	sort.Slice(out.Types, func(i, j int) bool { return out.Types[i].Name < out.Types[j].Name })
	sort.Slice(out.Allocations, func(i, j int) bool {
		if out.Allocations[i].File != out.Allocations[j].File {
			return out.Allocations[i].File < out.Allocations[j].File
		}
		return out.Allocations[i].Start < out.Allocations[j].Start
	})
	return out, nil
}

func run(input io.Reader, output io.Writer) error {
	decoder := json.NewDecoder(io.LimitReader(input, 65537))
	var body json.RawMessage
	if err := decoder.Decode(&body); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return errors.New("Go source request has trailing input")
	}
	var version struct {
		SchemaVersion int `json:"schema_version"`
	}
	if err := json.Unmarshal(body, &version); err != nil {
		return err
	}
	decode := json.NewDecoder(bytes.NewReader(body))
	decode.DisallowUnknownFields()
	var value inventory
	var err error
	switch version.SchemaVersion {
	case 1:
		var r request
		if err = decode.Decode(&r); err == nil {
			value, err = inspect(r)
		}
	case 2:
		var r taggedRequest
		if err = decode.Decode(&r); err == nil {
			value, err = inspectTagged(r)
		}
	default:
		return errors.New("unsupported Go inspection request version")
	}
	if err != nil {
		return err
	}
	return json.NewEncoder(output).Encode(value)
}

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
