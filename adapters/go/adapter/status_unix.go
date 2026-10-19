//go:build unix

package adapter

import (
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"syscall"
	"time"
)

// ChildSignal carries termination through Run so its deferred cleanup completes.
type ChildSignal struct{ Signal syscall.Signal }

func (failure *ChildSignal) Error() string {
	return fmt.Sprintf("child terminated by signal %d", failure.Signal)
}

func childExit(failure *exec.ExitError) (int, error) {
	if status, ok := failure.Sys().(syscall.WaitStatus); ok && status.Signaled() {
		return 0, &ChildSignal{status.Signal()}
	}
	return failure.ExitCode(), nil
}

// Terminate re-emits a signal after launcher-owned temporary files are removed.
func (failure *ChildSignal) Terminate() {
	signal.Reset(failure.Signal)
	if err := syscall.Kill(os.Getpid(), failure.Signal); err != nil {
		os.Exit(128 + int(failure.Signal))
	}
	// Go's signal handler can run on another thread. Give it time to restore
	// the kernel default and re-raise before using a bounded fallback.
	time.Sleep(time.Second)
	os.Exit(128 + int(failure.Signal))
}
