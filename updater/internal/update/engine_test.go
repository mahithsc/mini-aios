package update

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestBootstrapLatestInstallsFirstReleaseWithoutDrain(t *testing.T) {
	root := t.TempDir()
	now := time.Now().UTC()
	digest := "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	manifest := Manifest{
		SchemaVersion:         1,
		Product:               ProductName,
		ReleaseID:             "bootstrap-1",
		Version:               "1.0.0",
		Sequence:              1,
		Channel:               "stable",
		PublishedAt:           now.Add(-time.Minute),
		ExpiresAt:             now.Add(time.Hour),
		MinimumUpdaterVersion: "0.1.0",
		Revision:              "test-revision",
		Artifacts: map[string]Artifact{
			ContainerPlatform(): {
				Repository: "localhost:5000/mini-aios",
				Digest:     digest,
				SizeBytes:  1,
			},
		},
		Database: DatabasePolicy{
			FromSchemaMinimum:       1,
			FromSchemaMaximum:       1,
			ToSchema:                1,
			RestoreBackupOnRollback: true,
		},
		Policy: ReleasePolicy{
			DrainTimeoutSeconds:           1,
			StartupTimeoutSeconds:         2,
			ObservationSeconds:            1,
			ConsecutiveHealthFailureLimit: 1,
		},
	}
	writeSignedTestFeed(t, root, manifest)

	tokenPath := filepath.Join(root, "token")
	if err := os.WriteFile(tokenPath, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer test-token" {
			http.Error(writer, "unauthorized", http.StatusUnauthorized)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = fmt.Fprintf(
			writer,
			`{"status":"ready","releaseId":"%s","version":"%s","sequence":%d,"imageDigest":"%s","databaseSchema":1}`,
			manifest.ReleaseID,
			manifest.Version,
			manifest.Sequence,
			digest,
		)
	}))
	defer server.Close()

	commandLog := filepath.Join(root, "docker.log")
	fakeDocker := filepath.Join(root, "docker")
	script := fmt.Sprintf(`#!/bin/sh
printf '%%s\n' "$*" >> %q
if [ "$1" = image ] && [ "$2" = inspect ]; then
  printf '["localhost:5000/mini-aios@%s"]\n'
fi
`, commandLog, digest)
	if err := os.WriteFile(fakeDocker, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}

	config := testConfig(t, root)
	config.HealthURL = server.URL
	config.UpdaterTokenFile = tokenPath
	config.DockerBinary = fakeDocker
	config.MinimumFreeBytes = 1
	config.MaximumStartupValue = "3s"
	config.MaximumObservationValue = "2s"
	engine, err := NewEngine(config, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.BootstrapLatest(context.Background(), manifest.ReleaseID); err != nil {
		t.Fatal(err)
	}

	state, err := engine.Status()
	if err != nil {
		t.Fatal(err)
	}
	if state.Status != "idle" || state.Current == nil || state.Current.ReleaseID != manifest.ReleaseID {
		t.Fatalf("unexpected bootstrap state: %#v", state)
	}
	releaseEnvironment, err := os.ReadFile(config.ReleaseEnvPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(releaseEnvironment), "AIOS_IMAGE=localhost:5000/mini-aios@"+digest) {
		t.Fatalf("unexpected release environment: %s", releaseEnvironment)
	}
	commands, err := os.ReadFile(commandLog)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(commands), " stop ") || strings.Contains(string(commands), "/drain") {
		t.Fatalf("bootstrap attempted a normal update drain/stop transaction: %s", commands)
	}
	if !strings.Contains(string(commands), "compose") || !strings.Contains(string(commands), "up -d") {
		t.Fatalf("bootstrap did not start the compose service: %s", commands)
	}

	if err := engine.BootstrapLatest(context.Background(), manifest.ReleaseID); err != nil {
		t.Fatalf("idempotent second bootstrap returned %v", err)
	}
}

func TestFailedBootstrapClearsReleaseSelectionAndBootstrapState(t *testing.T) {
	root := t.TempDir()
	config := testConfig(t, root)
	config.DockerBinary = "/usr/bin/false"
	engine, err := NewEngine(config, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(config.ReleaseEnvPath, []byte("AIOS_RELEASE_ID=failed-bootstrap\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	target := ReleaseRef{ReleaseID: "failed-bootstrap"}
	state := State{Status: "activating", Target: &target, Bootstrap: true}
	if err := engine.failBootstrap(context.Background(), &state, fmt.Errorf("readiness failed")); err == nil {
		t.Fatal("failed bootstrap returned no error")
	}
	if _, err := os.Stat(config.ReleaseEnvPath); !os.IsNotExist(err) {
		t.Fatalf("release environment still exists after failed bootstrap: %v", err)
	}
	persisted, err := engine.Status()
	if err != nil {
		t.Fatal(err)
	}
	if persisted.Status != "failed" || persisted.Bootstrap || persisted.Target != nil {
		t.Fatalf("unexpected failed bootstrap state: %#v", persisted)
	}
}

func writeSignedTestFeed(t *testing.T, root string, manifest Manifest) {
	t.Helper()
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	publicDER, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(
		filepath.Join(root, "public.pem"),
		pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: publicDER}),
		0o600,
	); err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	envelope := Envelope{
		FormatVersion: 1,
		KeyID:         "test",
		Payload:       base64.StdEncoding.EncodeToString(payload),
		Signature:     base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload)),
	}
	encoded, err := json.Marshal(envelope)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "stable.json"), encoded, 0o600); err != nil {
		t.Fatal(err)
	}
}
