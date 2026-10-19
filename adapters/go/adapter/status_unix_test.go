//go:build unix

package adapter

import (
	"errors"
	"os"
	"os/exec"
	"strconv"
	"syscall"
	"testing"
)

func TestChildSignalTermination(t *testing.T) {
	if value := os.Getenv("OTELC_GO_TEST_CHILD_SIGNAL"); value != "" {
		number, err := strconv.Atoi(value)
		if err != nil {
			t.Fatal(err)
		}
		failure := &ChildSignal{syscall.Signal(number)}
		failure.Terminate()
		t.Fatal("signal termination returned")
	}
	for _, number := range []syscall.Signal{syscall.SIGTERM, syscall.SIGKILL} {
		command := exec.Command(os.Args[0], "-test.run=^TestChildSignalTermination$")
		command.Env = append(os.Environ(), "OTELC_GO_TEST_CHILD_SIGNAL="+strconv.Itoa(int(number)))
		err := command.Run()
		var exited *exec.ExitError
		if !errors.As(err, &exited) {
			t.Fatalf("expected a signalled child: %v", err)
		}
		code, err := childExit(exited)
		var signal *ChildSignal
		if code != 0 || !errors.As(err, &signal) || signal.Signal != number {
			t.Fatalf("lost signal %d: code=%d error=%v", number, code, err)
		}
		if signal.Error() == "" {
			t.Fatal("missing diagnostic")
		}
	}
}

func TestChildOrdinaryNonzeroExit(t *testing.T) {
	command := exec.Command("sh", "-c", "exit 7")
	var exited *exec.ExitError
	if !errors.As(command.Run(), &exited) {
		t.Fatal("expected exit 7")
	}
	if code, err := childExit(exited); code != 7 || err != nil {
		t.Fatalf("changed ordinary status: %d %v", code, err)
	}
}
