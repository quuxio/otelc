package main

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

func process_order(value int) int { return (value*17 + 3) % 97 }
func main() {
	fmt.Println("ready")
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "quit" {
			return
		}
		fields := strings.Fields(line)
		if len(fields) != 2 || fields[0] != "batch" {
			panic("invalid request")
		}
		count, err := strconv.Atoi(fields[1])
		if err != nil {
			panic(err)
		}
		started := time.Now()
		checksum := 0
		for index := 0; index < count; index++ {
			checksum += process_order(index)
		}
		fmt.Printf("elapsed_ns=%d checksum=%d calls=%d\n", time.Since(started).Nanoseconds(), checksum, count)
	}
}
