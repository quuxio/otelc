// Ordinary HTTP application. It has no telemetry imports or bootstrap code.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
)

var formats int
var retained []io.ReadCloser

type failure struct{}

func (*failure) Error() string { formats++; panic("application formatter must not run") }

func workloadChild() int { return 42 }

func workloadHandle(writer http.ResponseWriter, request *http.Request) {
	value := 42
	if request.URL.Path == "/child" {
		result := make(chan int, 1)
		go func() { result <- workloadChild() }()
		value = <-result
	}
	writer.Header().Set("X-Seen-Traceparent", request.Header.Get("traceparent"))
	fmt.Fprint(writer, value)
}

func workloadRequest(url, command string) (map[string]any, error) {
	ctx := context.Background()
	if command == "cancel" {
		cancelled, cancel := context.WithCancel(ctx)
		cancel()
		ctx = cancelled
	}
	request, _ := http.NewRequestWithContext(ctx, "GET", url+"/"+command, nil)
	request.Header.Set("traceparent", "unchanged-original-header")
	transport := http.DefaultTransport.(*http.Transport)
	originalFailure := &failure{}
	if command == "error" {
		transport = &http.Transport{DialContext: func(context.Context, string, string) (net.Conn, error) { return nil, originalFailure }}
		defer transport.CloseIdleConnections()
	}
	if command == "panic" {
		transport.RoundTrip(nil)
	}
	response, err := transport.RoundTrip(request)
	result := map[string]any{"original_header": request.Header.Get("traceparent"), "error_same": err == originalFailure}
	if err != nil {
		return result, err
	}
	result["wire_context"] = response.Header.Get("X-Seen-Traceparent")
	if command == "leak" {
		retained = append(retained, response.Body)
		result["value"] = "retained"
		return result, nil
	}
	data, err := io.ReadAll(response.Body)
	closeError := response.Body.Close()
	result["value"] = string(data)
	if command == "reuse" && err == nil && closeError == nil {
		next, requestError := http.NewRequestWithContext(response.Request.Context(), "GET", url+"/again", nil)
		if requestError != nil {
			return result, requestError
		}
		again, requestError := transport.RoundTrip(next)
		if requestError != nil {
			return result, requestError
		}
		data, err = io.ReadAll(again.Body)
		closeError = again.Body.Close()
		result["reused_value"] = string(data)
	}
	if err == nil {
		err = closeError
	}
	return result, err
}

func respond(url, command string) {
	defer func() {
		if recover() != nil {
			json.NewEncoder(os.Stdout).Encode(map[string]any{"panic": true, "formats": formats})
		}
	}()
	value, err := workloadRequest(url, command)
	json.NewEncoder(os.Stdout).Encode(map[string]any{"result": value, "error": err != nil, "formats": formats})
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "server" {
		server := httptest.NewServer(http.HandlerFunc(workloadHandle))
		defer server.Close()
		json.NewEncoder(os.Stdout).Encode(map[string]string{"url": server.URL})
		scanner := bufio.NewScanner(os.Stdin)
		for scanner.Scan() {
			if scanner.Text() == "quit" {
				return
			}
		}
		return
	}
	if len(os.Args) != 3 || os.Args[1] != "client" {
		panic("use server or client URL")
	}
	json.NewEncoder(os.Stdout).Encode(map[string]bool{"ready": true})
	scanner := bufio.NewScanner(os.Stdin)
	for scanner.Scan() {
		if scanner.Text() == "quit" {
			return
		}
		respond(os.Args[2], scanner.Text())
	}
}
