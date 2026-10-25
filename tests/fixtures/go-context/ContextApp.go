package main

import (
	"fmt"
	"os"
)

var release = make(chan struct{})
var result = make(chan int)

func workloadChild(seed int) int { return seed * 2 }

func workload(seed int) int {
	go func() { <-release; result <- workloadChild(seed) }()
	if os.Args[1] == "immediate" {
		close(release)
		return <-result
	}
	return seed
}

func main() {
	value := workload(21)
	if os.Args[1] == "delayed" {
		close(release)
		value = <-result
	}
	fmt.Println(value)
}
