// Ordinary seeded workload. No telemetry dependencies or instrumentation code.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"time"
)

type WorkItem struct{ value, total uint64 }

func (item *WorkItem) Close() { item.value, item.total = 0, 0 }

type LifecycleWorkItem struct {
	seed      int
	failClose bool
}

func newLifecycleWorkItem(seed int) (*LifecycleWorkItem, error) {
	if seed < 0 {
		return nil, errors.New("construction failed")
	}
	return &LifecycleWorkItem{seed: seed, failClose: seed == 1}, nil
}

func (item *LifecycleWorkItem) Close() error {
	if item.failClose {
		item.failClose = false
		return errors.New("close failed")
	}
	item.seed = 0
	return nil
}

func lifecycle() {
	_, construction := newLifecycleWorkItem(-1)
	item, _ := newLifecycleWorkItem(1)
	closeFailure := item.Close()
	_ = item.Close()
	_ = item.Close()
	_, _ = newLifecycleWorkItem(0)
	write(map[string]bool{"construction_failed": construction != nil, "close_failed": closeFailure != nil})
}

func workload(seed int) (uint64, error) {
	if seed < 0 {
		return 0, errors.New("negative seed")
	}
	item := &WorkItem{value: uint64(seed)}
	defer item.Close()
	for step := 0; step < 128; step++ {
		item.value = (item.value*1664525 + 1013904223) & 65535
		item.total += item.value
	}
	return item.total, nil
}

func excluded(seed uint64) uint64 { return seed }

func write(value any) {
	if err := json.NewEncoder(os.Stdout).Encode(value); err != nil {
		panic(err)
	}
}

func command(text string) bool {
	switch text {
	case "quit":
		return false
	case "gc":
		runtime.GC() // Explicit fixture command; instrumentation never requests GC.
		write(map[string]bool{"gc_requested": true})
	case "lifecycle":
		lifecycle()
	case "throw":
		_, err := workload(-1)
		write(map[string]string{"exception": "error", "message": err.Error()})
	default:
		count, err := strconv.Atoi(text)
		if err != nil || count < 1 || count > 1_000_000 {
			panic("count out of range")
		}
		start := time.Now()
		var checksum uint64
		for index := 0; index < count; index++ {
			value, err := workload(index)
			if err != nil {
				panic(err)
			}
			checksum += value + excluded(0)
		}
		write(map[string]any{"checksum": checksum, "elapsed_ns": time.Since(start).Nanoseconds(), "calls": count})
	}
	return true
}

func main() {
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		if !command(scanner.Text()) {
			return
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
