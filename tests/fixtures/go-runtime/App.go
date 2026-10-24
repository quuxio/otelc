package main

import (
	"bufio"
	"fmt"
	"os"
	"runtime"
	"strconv"
)

type WorkItem struct {
	seed, result int64
}

type appError struct{}

var payload = &appError{}
var formatters int

func (*appError) Error() string { formatters++; return "application failure" }

func newWorkItem() *WorkItem { return &WorkItem{seed: 21} }

func (item *WorkItem) close(mode int) error {
	if mode == 1 {
		return payload
	}
	if mode == 2 {
		panic(payload)
	}
	return nil
}

func workload(mode int) int64 {
	item := newWorkItem()
	_ = item.close(mode)
	_ = item.close(0)
	item.result = item.seed * 2
	return item.result
}

func main() {
	reader := bufio.NewScanner(os.Stdin)
	for reader.Scan() {
		text := reader.Text()
		if text == "quit" {
			return
		}
		if text == "gc" {
			runtime.GC() // Explicit test application command, never an injected hook.
			fmt.Println(`{"gc_requested":true}`)
			continue
		}
		mode, err := strconv.Atoi(text)
		if err != nil {
			panic(err)
		}
		var value int64
		func() {
			defer func() {
				if err := recover(); err != nil {
					if err != payload {
						panic("changed application panic identity")
					}
					value = 42
				}
			}()
			value = workload(mode)
		}()
		fmt.Printf("{\"value\":%d,\"formatters\":%d}\n", value, formatters)
	}
	if err := reader.Err(); err != nil {
		panic(err)
	}
}
