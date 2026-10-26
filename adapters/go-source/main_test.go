package main

import (
	"bytes"
	"encoding/json"
	"go/ast"
	"go/token"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/tools/go/packages"
)

func project(t *testing.T, files map[string]string) string {
	t.Helper()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	files["go.mod"] = "module example.com/original\n\ngo 1.26.0\n"
	for name, data := range files {
		path := filepath.Join(root, name)
		if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestActualTypeFactsPreserveOriginalInputsWithoutExecutingInitialisers(t *testing.T) {
	root := project(t, map[string]string{
		"main.go": `package main
import (
 renamed "example.com/original/model"
 "os"
 _ "embed"
)
//go:embed asset.txt
var asset string
func init(){os.WriteFile("executed",nil,0600)}
func main(){renamed.New()}
func Identity[T any](v T)T{return v}
func GenericError[T error](v T) T {return v}
func Blank()(_ error){return nil}
func unnamed() error{return nil}
func Named()(answer int, failure error){return}
func Values()(one,two int){return}
`,
		"model/model.go": `package model
type Object struct {Values [2]uint64}
type Small struct {Value byte}
type Alias = Object
type Generic[T any] struct {V T}
type Failure struct {Value [2]uint64}
func (f Failure) Error()string{return "do not invoke"}
type TypedError interface {error; Temporary()bool}
func New()(*Object,error){return &Object{},nil}
func Named()(value *Object, err error){return &Object{},nil}
func Allocations(){a:=new(Object);b:=&Object{};c:=&a.Values;_=a;_=b;_=c;var local Object; _=&local}
func(o *Object) Close()error{return nil}
func(o Object) Copy(){}
func(g *Generic[T]) Touch(){}
func ConcreteError()*Failure{return nil}
func ValueError()Failure{return Failure{}}
func InterfaceError()TypedError{return nil}
`,
		"asset.txt": "original embedded value",
	})
	before := map[string][]byte{}
	for _, name := range []string{"main.go", "model/model.go", "go.mod", "asset.txt"} {
		before[name], _ = os.ReadFile(filepath.Join(root, name))
	}
	out, err := inspect(request{1, root, "."})
	if err != nil {
		t.Fatal(err)
	}
	if out.Main != "example.com/original" || len(out.Files) != 2 || len(out.Types) != 6 || len(out.Functions) != 18 {
		t.Fatalf("unexpected inventory: %+v", out)
	}
	if len(out.CompilerInputs) != 3 || out.CompilerInputs["asset.txt"] != digest(before["asset.txt"]) {
		t.Fatal("compiler-selected embedded inputs missing", out.CompilerInputs)
	}
	facts := map[string]function{}
	for _, f := range out.Functions {
		facts[strings.TrimPrefix(f.Name, "example.com/original.")] = f
		if f.Line < 1 || f.SHA256 != out.Files[f.File] {
			t.Fatalf("unbound location: %+v", f)
		}
	}
	for _, key := range []string{"init", "Blank", "GenericError"} {
		if facts[key].Gap == "" {
			t.Fatalf("missing unsupported binding for %s", key)
		}
	}
	if !facts["Identity"].Generic || !facts["example.com/original/model.Generic.Touch"].Generic {
		t.Fatal("generic type information lost")
	}
	unnamed := facts["unnamed"].Results[0]
	if unnamed.Expression != "_unnamedRetVal0" || unnamed.Failure != "_unnamedRetVal0 != nil" || !unnamed.Error {
		t.Fatal(unnamed)
	}
	if facts["Named"].Results[1].Failure != "failure != nil" || facts["Values"].Results[1].Expression != "two" {
		t.Fatal("named result binding lost")
	}
	for _, name := range []string{"ConcreteError", "InterfaceError"} {
		if facts["example.com/original/model."+name].Results[0].Failure != "_unnamedRetVal0 != nil" {
			t.Fatal("concrete/extended error binding lost", name)
		}
	}
	if facts["example.com/original/model.ValueError"].Results[0].Failure != "true" {
		t.Fatal("non-nilable error value must not generate an invalid nil comparison")
	}
	factory := facts["example.com/original/model.New"]
	if factory.Results[0].Pointer != "example.com/original/model.Object" || factory.Results[1].Failure == "" {
		t.Fatal(factory)
	}
	close := facts["example.com/original/model.Object.Close"]
	if close.Receiver != "o" || close.ReceiverID != "*Object" || close.Parameters != 0 {
		t.Fatal(close)
	}
	for _, typ := range out.Types {
		if strings.HasSuffix(typ.Name, ".Object") && (typ.Size != 16 || typ.Gap != "") {
			t.Fatal(typ)
		}
		if (strings.HasSuffix(typ.Name, ".Small") || strings.HasSuffix(typ.Name, ".Alias") || strings.HasSuffix(typ.Name, ".Generic")) && typ.Gap == "" {
			t.Fatal("unsupported lifetime incorrectly admitted", typ)
		}
	}
	for name, data := range before {
		after, _ := os.ReadFile(filepath.Join(root, name))
		if !bytes.Equal(data, after) {
			t.Fatalf("original input changed: %s", name)
		}
	}
	if len(out.Allocations) != 5 {
		t.Fatalf("wrong allocation denominator: %+v", out.Allocations)
	}
	kinds := map[string]int{}
	for _, row := range out.Allocations {
		kinds[row.Kind]++
		raw := before[row.File][row.Start:row.End]
		if len(raw) == 0 || row.Type != "example.com/original/model.Object" || row.SHA256 != out.Files[row.File] {
			t.Fatal("unbound original allocation", row, string(raw))
		}
		if (row.Kind == "address") != (row.Gap != "") {
			t.Fatal("storage-address gap lost", row)
		}
	}
	if kinds["new"] != 1 || kinds["composite"] != 3 || kinds["address"] != 1 {
		t.Fatal(kinds)
	}
	if _, err := os.Stat(filepath.Join(root, "executed")); !os.IsNotExist(err) {
		t.Fatal("application initialiser was executed")
	}
}

func TestRequestsAndCompilerFailuresRemainErrors(t *testing.T) {
	root := project(t, map[string]string{"main.go": "package main\nfunc main(){}\n"})
	for _, target := range []string{"", "..", "../other", "./../other", "./...", "./a/../b", "/tmp", "-x", "./"} {
		if validate(request{1, root, target}) == nil {
			t.Fatal("accepted target", target)
		}
	}
	for _, r := range []request{{2, root, "."}, {1, ".", "."}, {1, root + "/.", "."}, {1, root + "/missing", "."}} {
		if _, err := inspect(r); err == nil {
			t.Fatal("accepted invalid request", r)
		}
	}
	alias := filepath.Join(t.TempDir(), "alias")
	if err := os.Symlink(root, alias); err != nil {
		t.Fatal(err)
	}
	if validate(request{1, alias, "."}) == nil {
		t.Fatal("accepted root alias")
	}
	for _, data := range []string{"{", `{ "schema_version":1,"extra":true}`, `{} {}`, `{}` + strings.Repeat(" ", 65540)} {
		if run(strings.NewReader(data), &bytes.Buffer{}) == nil {
			t.Fatal("accepted malformed request")
		}
	}
	var input, output bytes.Buffer
	json.NewEncoder(&input).Encode(request{1, root, "."})
	if err := run(&input, &output); err != nil || !strings.Contains(output.String(), `"main":"example.com/original"`) {
		t.Fatal(err, output.String())
	}
	for _, source := range []string{"package library\n", "package main\nfunc main(){missing()}\n", "package main\nfunc"} {
		if err := os.WriteFile(filepath.Join(root, "main.go"), []byte(source), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := inspect(request{1, root, "."}); err == nil {
			t.Fatal("accepted invalid build target", source)
		}
	}
}

func TestSourceBoundaryAndParserIdentityCannotBeInvented(t *testing.T) {
	root := project(t, map[string]string{"main.go": "package main\n"})
	if _, err := inside(root, filepath.Join(filepath.Dir(root), "foreign.go")); err == nil {
		t.Fatal("accepted foreign source")
	}
	link := filepath.Join(root, "alias.go")
	if err := os.Symlink(filepath.Join(root, "main.go"), link); err != nil {
		t.Fatal(err)
	}
	if _, err := inside(root, link); err == nil {
		t.Fatal("accepted source alias")
	}
	fset := token.NewFileSet()
	tok := fset.AddFile(filepath.Join(root, "main.go"), -1, 13)
	p := &packages.Package{Fset: fset}
	file := &ast.File{Package: tok.Pos(0), Name: &ast.Ident{Name: "main"}}
	if err := scanFile(request{1, root, "."}, p, file, map[string]string{}, &inventory{}); err == nil {
		t.Fatal("accepted unobserved parser input")
	}
}
