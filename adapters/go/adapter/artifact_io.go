package adapter

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
)

const metadataLimit = 4 * 1024 * 1024
const binaryLimit = 256 * 1024 * 1024

func fileHash(path string) (string, error) {
	info, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() || info.Size() > binaryLimit {
		return "", fmt.Errorf("go build input must be a regular file of at most 256 MiB")
	}
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	digest := sha256.New()
	count, err := io.Copy(digest, io.LimitReader(file, binaryLimit+1))
	if err != nil {
		return "", err
	}
	if count > binaryLimit {
		return "", fmt.Errorf("go build input exceeds 256 MiB")
	}
	return hex.EncodeToString(digest.Sum(nil)), nil
}

func readBounded(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > metadataLimit {
		return nil, fmt.Errorf("go bundle metadata must be a regular file of at most 4 MiB")
	}
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, metadataLimit+1))
	if err == nil && len(data) > metadataLimit {
		err = fmt.Errorf("go bundle metadata exceeds 4 MiB")
	}
	return data, err
}

func decodeStrict(data []byte, destination any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return fmt.Errorf("go bundle metadata must contain one JSON document")
	}
	return nil
}

func writePrivate(path string, data []byte) error {
	if len(data) > metadataLimit {
		return fmt.Errorf("go bundle metadata exceeds 4 MiB")
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	_, failure := file.Write(data)
	err = file.Close()
	if failure != nil {
		return failure
	}
	return err
}

func copyExecutable(source, destination, expected string) error {
	info, err := os.Lstat(source)
	if err != nil {
		return err
	}
	if !info.Mode().IsRegular() || info.Size() > binaryLimit || info.Mode().Perm()&0111 == 0 {
		return fmt.Errorf("go artifact executable must be a regular executable of at most 256 MiB")
	}
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(destination, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0500)
	if err != nil {
		return err
	}
	_, failure := io.Copy(out, io.LimitReader(in, binaryLimit+1))
	err = out.Close()
	if failure != nil {
		return failure
	}
	if err != nil {
		return err
	}
	digest, err := fileHash(destination)
	if err != nil || digest != expected {
		return fmt.Errorf("go artifact executable fingerprint mismatch")
	}
	return nil
}
