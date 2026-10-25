package main

import (
	"fmt"
	"os"
	"runtime"
	"time"
)

type Object struct{ storage [32]byte }
type appError struct{}

var formatters int
var payload = &appError{}

func (*appError) Error() string { formatters++; return "application error" }

func NewObject() *Object { return &Object{} }
func (o *Object) Close(mode string) error {
	if mode == "panic" {
		panic(payload)
	}
	if mode == "failed" {
		return payload
	}
	return nil
}
func child(value int) int { return value * 2 }
func workload(mode string) int {
	object := NewObject()
	if mode == "failed" {
		_ = object.Close(mode)
	}
	_ = object.Close(mode)
	_ = object.Close("normal")
	return child(21)
}
func main() {
	mode := os.Args[1]
	value := 0
	func() {
		defer func() {
			if item := recover(); item != nil {
				if item != payload {
					panic("changed panic identity")
				}
				value = 42
			}
		}()
		value = workload(mode)
	}()
	// Explicit fixture collection only. Instrumentation never requests GC.
	for n := 0; n < 20; n++ {
		runtime.GC()
		time.Sleep(time.Millisecond)
	}
	fmt.Println(value, formatters)
}
