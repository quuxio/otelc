package main

import (
	"fmt"
	"sync"
)

func process_order(value int) int { return value * 3 }
func recursive(depth int) int {
	if depth == 0 {
		return 0
	}
	return 1 + recursive(depth-1)
}
func escaping() { panic("escaping") }
func caught() (value int) {
	defer func() {
		if recover() != nil {
			value = 7
		}
	}()
	panic("caught")
}

type Order struct{ value int }

func (order Order) calculate(other int) int { return order.value + other }
func main() {
	result := process_order(4) + recursive(3) + caught() + Order{4}.calculate(5)
	func() {
		defer func() {
			if value := recover(); value != nil {
				result += len(value.(string))
			}
		}()
		escaping()
	}()
	var wait sync.WaitGroup
	results := make(chan int, 2)
	for _, value := range []int{5, 6} {
		wait.Go(func() { results <- process_order(value) })
	}
	wait.Wait()
	close(results)
	for value := range results {
		result += value
	}
	fmt.Println(result)
}
