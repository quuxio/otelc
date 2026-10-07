package runtime

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

type control struct {
	listener *net.UnixListener
	filename string
	identity os.FileInfo
}

func bind(rt *Runtime, filename string) (*control, error) {
	parent, err := os.Lstat(filepath.Dir(filename))
	if err != nil {
		return nil, err
	}
	stat, ok := parent.Sys().(*syscall.Stat_t)
	if !ok || !parent.IsDir() || parent.Mode().Perm() != 0700 || int(stat.Uid) != os.Getuid() {
		return nil, fmt.Errorf("control parent must be an owner-only directory")
	}
	if _, err = os.Lstat(filename); !os.IsNotExist(err) {
		return nil, fmt.Errorf("control socket already exists")
	}
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: filename, Net: "unix"})
	if err != nil {
		return nil, err
	}
	listener.SetUnlinkOnClose(false)
	if err = os.Chmod(filename, 0600); err != nil {
		listener.Close()
		os.Remove(filename)
		return nil, err
	}
	identity, err := os.Lstat(filename)
	if err != nil {
		listener.Close()
		os.Remove(filename)
		return nil, err
	}
	c := &control{listener: listener, filename: filename, identity: identity}
	go func() {
		for {
			connection, err := listener.AcceptUnix()
			if err != nil {
				return
			}
			c.handle(rt, connection)
		}
	}()
	return c, nil
}
func (c *control) handle(rt *Runtime, connection net.Conn) {
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(200 * time.Millisecond))
	request, err := bufio.NewReader(io.LimitReader(connection, 18)).ReadString('\n')
	connection.SetWriteDeadline(time.Now().Add(200 * time.Millisecond))
	if err != nil || len(request) > 16 {
		json.NewEncoder(connection).Encode(map[string]string{"error": "invalid control request"})
		return
	}
	if request == "enable\n" {
		rt.enabled.Store(true)
	} else if request == "disable\n" {
		rt.enabled.Store(false)
	} else if request != "status\n" {
		json.NewEncoder(connection).Encode(map[string]string{"error": "invalid control request"})
		return
	}
	report := rt.report()
	json.NewEncoder(connection).Encode(map[string]any{"schema_version": 1, "pid": os.Getpid(), "metrics_enabled": rt.enabled.Load(), "function_calls": report["function_calls"]})
}
func (c *control) close() {
	c.listener.Close()
	if current, err := os.Lstat(c.filename); err == nil && os.SameFile(current, c.identity) {
		os.Remove(c.filename)
	}
}
