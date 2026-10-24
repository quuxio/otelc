package main

import (
	"bufio"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
)

var downstream string

func workloadRelay(url string) map[string]string {
	response, err := http.Get(url)
	if err != nil {
		panic(err)
	}
	defer response.Body.Close()
	var value map[string]string
	if err := json.NewDecoder(response.Body).Decode(&value); err != nil {
		panic(err)
	}
	return value
}

func workloadHandle(w http.ResponseWriter, r *http.Request) {
	value := map[string]string{"value": "42", "traceparent": r.Header.Get("traceparent"), "baggage": r.Header.Get("baggage")}
	if downstream != "" {
		if r.URL.Query().Has("child") {
			result := make(chan map[string]string, 1)
			go func() { result <- workloadRelay(downstream) }()
			value = <-result
		} else {
			value = workloadRelay(downstream)
		}
		value["incoming_baggage"] = r.Header.Get("baggage")
	}
	if err := json.NewEncoder(w).Encode(value); err != nil {
		panic(err)
	}
}

func main() {
	if os.Args[1] == "relay" {
		downstream = os.Args[2]
	}
	server := httptest.NewServer(http.HandlerFunc(workloadHandle))
	defer server.Close()
	if err := json.NewEncoder(os.Stdout).Encode(map[string]string{"url": server.URL}); err != nil {
		panic(err)
	}
	input := bufio.NewScanner(os.Stdin)
	for input.Scan() {
		if input.Text() == "quit" {
			return
		}
	}
}
