// Package adapter generates Go compiler overlays while retaining original input.
package adapter

import (
	"bytes"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"io.quux.otelc/go/policy"
)

const marker = "quux.otelc.generated"

type Function struct {
	Name     string `json:"name"`
	Selected bool   `json:"selected"`
	Line     int    `json:"line"`
}
type insertion struct {
	offset int
	code   string
}

func annotations(comments *ast.CommentGroup, read bool) (bool, bool, error) {
	if !read || comments == nil {
		return false, false, nil
	}
	include, exclude := false, false
	for _, comment := range comments.List {
		value := strings.TrimSpace(strings.TrimSuffix(strings.TrimPrefix(strings.TrimPrefix(comment.Text, "//"), "/*"), "*/"))
		if value == "otelc.instrument" {
			include = true
		} else if value == "otelc.exclude" {
			exclude = true
		} else if strings.HasPrefix(value, "otelc.") {
			return false, false, fmt.Errorf("unknown Go instrumentation annotation")
		}
	}
	return include, exclude, nil
}
func receiver(value ast.Expr) string {
	switch node := value.(type) {
	case *ast.Ident:
		return node.Name
	case *ast.StarExpr:
		return receiver(node.X)
	case *ast.IndexExpr:
		return receiver(node.X)
	case *ast.IndexListExpr:
		return receiver(node.X)
	default:
		return "<receiver>"
	}
}
func Transform(filename string, source []byte, packageName string, plan policy.Plan, sourceSelected bool) ([]byte, []Function, error) {
	if bytes.Contains(source, []byte(marker)) {
		return nil, nil, fmt.Errorf("go input is already generated")
	}
	positions := token.NewFileSet()
	file, err := parser.ParseFile(positions, filename, source, parser.ParseComments)
	if err != nil {
		return nil, nil, err
	}
	used := map[string]bool{}
	ast.Inspect(file, func(node ast.Node) bool {
		if name, ok := node.(*ast.Ident); ok {
			used[name.Name] = true
		}
		return true
	})
	alias := "__quux_otelc"
	for used[alias] {
		alias += "_"
	}
	functions := []Function{}
	edits := []insertion{}
	seen := map[*ast.FuncLit]bool{}
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			continue
		}
		name := packageName + "."
		if fn.Recv != nil {
			name += receiver(fn.Recv.List[0].Type) + "."
		}
		name += fn.Name.Name
		if fn.Name.Name == "init" {
			name += "@" + filepath.Base(filename) + ":" + strconv.Itoa(positions.Position(fn.Pos()).Line)
		}
		include, exclude, err := annotations(fn.Doc, plan.Annotations.Read && sourceSelected)
		if err != nil {
			return nil, nil, err
		}
		selected := sourceSelected && !exclude && plan.Functions.Accept(name, include)
		functions = append(functions, Function{Name: name, Selected: selected, Line: positions.Position(fn.Pos()).Line})
		code := ""
		if file.Name.Name == "main" && fn.Name.Name == "main" {
			code = "defer " + alias + ".Close();"
		}
		if selected {
			code += "defer " + alias + ".Finish(" + alias + ".Start(" + strconv.Quote(name) + "));"
		}
		if code != "" {
			edits = append(edits, insertion{positions.Position(fn.Body.Lbrace).Offset + 1, code})
		}
		ast.Inspect(fn.Body, func(node ast.Node) bool {
			literal, ok := node.(*ast.FuncLit)
			if !ok {
				return true
			}
			seen[literal] = true
			line := positions.Position(literal.Pos()).Line
			anonymous := name + ".<anonymous>:" + strconv.Itoa(line) + ":" + strconv.Itoa(positions.Position(literal.Pos()).Column)
			var doc *ast.CommentGroup
			for _, group := range file.Comments {
				if positions.Position(group.End()).Line == line-1 && group.End() < literal.Pos() {
					doc = group
				}
			}
			inc, exc, failure := annotations(doc, plan.Annotations.Read && sourceSelected)
			if failure != nil {
				err = failure
				return false
			}
			admit := sourceSelected && !exc && plan.Functions.Accept(anonymous, inc)
			functions = append(functions, Function{Name: anonymous, Selected: admit, Line: line})
			if admit {
				edits = append(edits, insertion{positions.Position(literal.Body.Lbrace).Offset + 1, "defer " + alias + ".Finish(" + alias + ".Start(" + strconv.Quote(anonymous) + "));"})
			}
			return true
		})
		if err != nil {
			return nil, nil, err
		}
	}
	// Package-level callbacks also have bodies; keep their original line identity.
	ast.Inspect(file, func(node ast.Node) bool {
		literal, ok := node.(*ast.FuncLit)
		if !ok || seen[literal] {
			return true
		}
		position := positions.Position(literal.Pos())
		name := packageName + ".<anonymous>:" + strconv.Itoa(position.Line) + ":" + strconv.Itoa(position.Column)
		selected := sourceSelected && plan.Functions.Accept(name, false)
		functions = append(functions, Function{Name: name, Selected: selected, Line: position.Line})
		if selected {
			edits = append(edits, insertion{positions.Position(literal.Body.Lbrace).Offset + 1, "defer " + alias + ".Finish(" + alias + ".Start(" + strconv.Quote(name) + "));"})
		}
		return true
	})
	if len(edits) == 0 {
		return source, functions, nil
	}
	annotation := ""
	if plan.Annotations.Inject {
		annotation = "/*otelc.instrument*/"
	}
	at := positions.Position(file.Name.End())
	edits = append(edits, insertion{at.Offset, "\nimport " + alias + " \"io.quux.otelc/go/runtime\" // " + marker + "\n//line " + filename + ":" + strconv.Itoa(at.Line) + "\n"})
	sort.SliceStable(edits, func(i, j int) bool { return edits[i].offset > edits[j].offset })
	output := append([]byte(nil), source...)
	for _, edit := range edits {
		value := edit.code
		if edit.offset != at.Offset {
			value = annotation + value
		}
		output = append(output[:edit.offset], append([]byte(value), output[edit.offset:]...)...)
	}
	return output, functions, nil
}
