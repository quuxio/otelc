//go:build !unix

package adapter

import "os/exec"

// ChildSignal has no producer on platforms without Unix wait statuses.
type ChildSignal struct{}

func (*ChildSignal) Error() string                   { return "child terminated" }
func (*ChildSignal) Terminate()                      {}
func childExit(failure *exec.ExitError) (int, error) { return failure.ExitCode(), nil }
