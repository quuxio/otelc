package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"sync"
)

type Settings struct {
	URL         string `json:"url"`
	Certificate string `json:"certificate"`
}
type Reply struct {
	Value    string `json:"value"`
	Protocol string `json:"protocol"`
	Header   string `json:"header"`
	Original string `json:"original"`
}

func workloadHandle(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Traceparent", r.Header.Get("traceparent"))
	fmt.Fprint(w, "42")
}
func workloadRequest(transport *http.Transport, url string) Reply {
	request, err := http.NewRequestWithContext(context.Background(), "GET", url, nil)
	if err != nil {
		panic(err)
	}
	request.Header.Set("traceparent", "original")
	response, err := transport.RoundTrip(request)
	if err != nil {
		panic(err)
	}
	data, err := io.ReadAll(response.Body)
	if err != nil {
		panic(err)
	}
	if err = response.Body.Close(); err != nil {
		panic(err)
	}
	return Reply{string(data), response.Proto, response.Header.Get("X-Traceparent"), request.Header.Get("traceparent")}
}
func main() {
	if os.Args[1] == "server" {
		server := httptest.NewUnstartedServer(http.HandlerFunc(workloadHandle))
		server.EnableHTTP2 = true
		server.StartTLS()
		defer server.Close()
		json.NewEncoder(os.Stdout).Encode(Settings{server.URL, base64.StdEncoding.EncodeToString(server.Certificate().Raw)})
		input := bufio.NewScanner(os.Stdin)
		for input.Scan() {
			if input.Text() == "quit" {
				return
			}
		}
		return
	}
	var settings Settings
	if err := json.Unmarshal([]byte(os.Args[2]), &settings); err != nil {
		panic(err)
	}
	der, err := base64.StdEncoding.DecodeString(settings.Certificate)
	if err != nil {
		panic(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		panic(err)
	}
	roots := x509.NewCertPool()
	roots.AddCert(cert)
	transport := &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}, ForceAttemptHTTP2: true}
	defer transport.CloseIdleConnections()
	json.NewEncoder(os.Stdout).Encode(map[string]bool{"ready": true})
	input := bufio.NewScanner(os.Stdin)
	for input.Scan() {
		if input.Text() == "quit" {
			return
		}
		count := 1
		if input.Text() == "concurrent" {
			count = 4
		}
		replies := make([]Reply, count)
		var pending sync.WaitGroup
		for index := range replies {
			pending.Add(1)
			go func() { defer pending.Done(); replies[index] = workloadRequest(transport, settings.URL) }()
		}
		pending.Wait()
		json.NewEncoder(os.Stdout).Encode(replies)
	}
}
