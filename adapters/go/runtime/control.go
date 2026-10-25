package runtime

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

type controlServer struct {
	runtime  *Runtime
	listener net.Listener
	token    string
	ctx      context.Context
	cancel   context.CancelFunc
	wait     sync.WaitGroup
	mu       sync.Mutex
	clients  map[net.Conn]bool
	closing  bool
	command  chan struct{}
}

func newControl(r *Runtime) (*controlServer, error) {
	var token [32]byte
	if _, err := rand.Read(token[:]); err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	s := &controlServer{runtime: r, listener: listener, token: hex.EncodeToString(token[:]), ctx: ctx, cancel: cancel,
		clients: make(map[net.Conn]bool), command: make(chan struct{}, 1)}
	s.command <- struct{}{}
	descriptor := map[string]any{"schema_version": 1, "kind": "otelc.control.tcp", "host": "127.0.0.1",
		"port": listener.Addr().(*net.TCPAddr).Port, "pid": os.Getpid(), "token": s.token}
	if err := createJSON(r.directory, "control.json", descriptor); err != nil {
		listener.Close()
		cancel()
		return nil, err
	}
	s.wait.Add(1)
	go s.serve()
	return s, nil
}

func (s *controlServer) serve() {
	defer s.wait.Done()
	for {
		client, err := s.listener.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		if s.closing || len(s.clients) >= 4 {
			s.mu.Unlock()
			client.Close()
			continue
		}
		s.clients[client] = true
		s.wait.Add(1)
		s.mu.Unlock()
		go s.handle(client)
	}
}

func (s *controlServer) handle(client net.Conn) {
	defer s.wait.Done()
	defer func() { client.Close(); s.mu.Lock(); delete(s.clients, client); s.mu.Unlock() }()
	_ = client.SetDeadline(time.Now().Add(2500 * time.Millisecond))
	line, err := bufio.NewReader(io.LimitReader(client, 4097)).ReadString('\n')
	if err != nil || len(line) > 4096 || !strings.HasSuffix(line, "\n") {
		return
	}
	var request struct {
		Token   string `json:"token"`
		Command string `json:"command"`
	}
	if decodeObject([]byte(line), []string{"token", "command"}, &request) != nil ||
		subtle.ConstantTimeCompare([]byte(request.Token), []byte(s.token)) != 1 {
		return
	}
	ctx, cancel := context.WithTimeout(s.ctx, 2*time.Second)
	defer cancel()
	response, err := s.run(ctx, request.Command)
	if err != nil {
		response = map[string]any{"schema_version": 1, "pid": os.Getpid(), "error": "control operation failed"}
	}
	_ = json.NewEncoder(client).Encode(response)
}

func (s *controlServer) run(ctx context.Context, command string) (map[string]any, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-s.command:
	}
	defer func() { s.command <- struct{}{} }()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	r := s.runtime
	if command == "status" {
		return r.Status(), nil
	}
	if command == "health" {
		return r.Health(), nil
	}
	if command == "flush" {
		err := r.Flush(ctx)
		return r.Status(), err
	}
	collection, emission := r.Controls.CollectionEnabled(), r.Controls.EmissionEnabled()
	switch command {
	case "on":
		collection, emission = true, true
	case "off":
		collection, emission = false, false
	case "collection on":
		collection = true
	case "collection off":
		collection = false
	case "emission on":
		emission = true
	case "emission off":
		emission = false
	default:
		return nil, errors.New("unsupported Go control command")
	}
	// Collection-only changes must not wait for unrelated network export. The
	// explicit emission/off commands retain the strict in-flight export barrier.
	if !strings.HasPrefix(command, "collection ") {
		if err := r.Controls.SetEmission(ctx, emission); err != nil {
			return nil, err
		}
	}
	r.Controls.SetCollection(collection)
	return r.Status(), nil
}

func (s *controlServer) close() {
	s.mu.Lock()
	s.closing = true
	s.cancel()
	_ = s.listener.Close()
	for client := range s.clients {
		_ = client.Close()
	}
	s.mu.Unlock()
	s.wait.Wait()
}
