package update

import (
	"context"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

const maximumFeedBytes = 2 * 1024 * 1024

type FeedClient struct {
	HTTPClient *http.Client
	Now        func() time.Time
}

func (f FeedClient) Fetch(ctx context.Context, config Config) (Manifest, error) {
	data, err := f.fetchBytes(ctx, config.FeedURL)
	if err != nil {
		return Manifest{}, err
	}
	var envelope Envelope
	if err := json.Unmarshal(data, &envelope); err != nil {
		return Manifest{}, fmt.Errorf("decode signed feed: %w", err)
	}
	if envelope.FormatVersion != FeedFormatVersion {
		return Manifest{}, fmt.Errorf("unsupported feed envelope %d", envelope.FormatVersion)
	}
	payload, err := base64.StdEncoding.DecodeString(envelope.Payload)
	if err != nil {
		return Manifest{}, fmt.Errorf("decode feed payload: %w", err)
	}
	signature, err := base64.StdEncoding.DecodeString(envelope.Signature)
	if err != nil {
		return Manifest{}, fmt.Errorf("decode feed signature: %w", err)
	}
	publicKey, err := loadPublicKey(config.PublicKeyPath)
	if err != nil {
		return Manifest{}, err
	}
	if !ed25519.Verify(publicKey, payload, signature) {
		return Manifest{}, fmt.Errorf("feed signature verification failed")
	}
	var manifest Manifest
	if err := json.Unmarshal(payload, &manifest); err != nil {
		return Manifest{}, fmt.Errorf("decode verified manifest: %w", err)
	}
	now := time.Now().UTC()
	if f.Now != nil {
		now = f.Now().UTC()
	}
	if err := manifest.Validate(config, now); err != nil {
		return Manifest{}, err
	}
	return manifest, nil
}

func (f FeedClient) fetchBytes(ctx context.Context, location string) ([]byte, error) {
	parsed, err := url.Parse(location)
	if err != nil {
		return nil, err
	}
	if parsed.Scheme == "file" || parsed.Scheme == "" {
		path := location
		if parsed.Scheme == "file" {
			path = parsed.Path
		}
		info, err := os.Stat(path)
		if err != nil {
			return nil, fmt.Errorf("open feed: %w", err)
		}
		if info.Size() > maximumFeedBytes {
			return nil, fmt.Errorf("feed exceeds maximum size")
		}
		return os.ReadFile(path)
	}
	if parsed.Scheme != "https" && !(parsed.Scheme == "http" && isLoopbackHost(parsed.Hostname())) {
		return nil, fmt.Errorf("feed URL must use HTTPS or loopback HTTP")
	}
	client := f.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, location, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "application/json")
	response, err := client.Do(request)
	if err != nil {
		return nil, fmt.Errorf("fetch feed: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch feed returned HTTP %d", response.StatusCode)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maximumFeedBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > maximumFeedBytes {
		return nil, fmt.Errorf("feed exceeds maximum size")
	}
	return data, nil
}

func loadPublicKey(path string) (ed25519.PublicKey, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read update public key: %w", err)
	}
	if block, _ := pem.Decode(data); block != nil {
		parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse update public key: %w", err)
		}
		key, ok := parsed.(ed25519.PublicKey)
		if !ok {
			return nil, fmt.Errorf("update public key is not Ed25519")
		}
		return key, nil
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(data)))
	if err != nil || len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("update public key must be Ed25519 PEM or base64")
	}
	return ed25519.PublicKey(raw), nil
}

func isLoopbackHost(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1"
}
