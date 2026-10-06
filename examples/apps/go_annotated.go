package main

import "fmt"

// otelc.instrument
func selected(value int) int   { return value * 3 }
func configured(value int) int { return value + 7 }

// otelc.exclude
func excluded(value int) int { return value - 1 }
func main()                  { fmt.Println(selected(4) + configured(10) + excluded(2)) }
