package main

import (
	"fmt"
	"os"
	"path/filepath"

	"io.quux.otelc/go/adapter"
)

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "otelc Go requires a resolved policy")
		os.Exit(1)
	}
	binary, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	root := filepath.Dir(filepath.Dir(binary))
	code, err := adapter.Run(os.Args[1], os.Args[2:], root)
	if err != nil {
		fmt.Fprintln(os.Stderr, "otelc Go:", err)
		code = 1
	}
	os.Exit(code)
}
