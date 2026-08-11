package update

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func testConfig(t *testing.T, root string) Config {
	t.Helper()
	return Config{
		Channel:                 "stable",
		FeedURL:                 "file://" + filepath.Join(root, "stable.json"),
		PublicKeyPath:           filepath.Join(root, "public.pem"),
		AllowedImageRepository:  "localhost:5000/mini-aios",
		ComposeProjectDir:       root,
		ComposeService:          "box",
		ReleaseEnvPath:          filepath.Join(root, "release.env"),
		AIOSDataDir:             filepath.Join(root, "data"),
		DatabaseRelativePath:    "state/aios.db",
		StateDir:                filepath.Join(root, "updater-state"),
		HealthURL:               "http://127.0.0.1:8765/internal/updater",
		UpdaterTokenFile:        filepath.Join(root, "token"),
		DockerBinary:            "docker",
		PollIntervalValue:       "1m",
		PollJitterValue:         "0s",
		MaximumDrainValue:       "10m",
		MaximumStartupValue:     "5m",
		MaximumObservationValue: "30m",
		ClockSkewValue:          "5m",
		AllowDevelopmentHost:    true,
	}
}

func TestFeedVerifiesExactSignedPayload(t *testing.T) {
	root := t.TempDir()
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	publicDER, _ := x509.MarshalPKIXPublicKey(publicKey)
	if err := os.WriteFile(filepath.Join(root, "public.pem"), pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: publicDER}), 0o600); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	digest := "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	manifest := Manifest{
		SchemaVersion:         1,
		Product:               ProductName,
		ReleaseID:             "2026.08.10.1",
		Version:               "0.2.0",
		Sequence:              2,
		Channel:               "stable",
		PublishedAt:           now.Add(-time.Hour),
		ExpiresAt:             now.Add(time.Hour),
		MinimumUpdaterVersion: "0.1.0",
		Artifacts:             map[string]Artifact{ContainerPlatform(): {Repository: "localhost:5000/mini-aios", Digest: digest, SizeBytes: 1}},
		Database:              DatabasePolicy{FromSchemaMinimum: 1, FromSchemaMaximum: 1, ToSchema: 1, PreviousAppCanReadToSchema: true},
		Policy:                ReleasePolicy{DrainTimeoutSeconds: 10, StartupTimeoutSeconds: 10, ObservationSeconds: 10, ConsecutiveHealthFailureLimit: 1},
	}
	payload, _ := json.Marshal(manifest)
	envelope := Envelope{
		FormatVersion: 1,
		KeyID:         "test",
		Payload:       base64.StdEncoding.EncodeToString(payload),
		Signature:     base64.StdEncoding.EncodeToString(ed25519.Sign(privateKey, payload)),
	}
	encoded, _ := json.Marshal(envelope)
	if err := os.WriteFile(filepath.Join(root, "stable.json"), encoded, 0o600); err != nil {
		t.Fatal(err)
	}
	client := FeedClient{Now: func() time.Time { return now }}
	verified, err := client.Fetch(context.Background(), testConfig(t, root))
	if err != nil {
		t.Fatal(err)
	}
	if verified.ReleaseID != manifest.ReleaseID {
		t.Fatalf("release ID = %s", verified.ReleaseID)
	}

	envelope.Payload = base64.StdEncoding.EncodeToString(append(payload, ' '))
	tampered, _ := json.Marshal(envelope)
	_ = os.WriteFile(filepath.Join(root, "stable.json"), tampered, 0o600)
	if _, err := client.Fetch(context.Background(), testConfig(t, root)); err == nil {
		t.Fatal("tampered payload was accepted")
	}
}

func TestManifestRejectsWrongRepositoryAndExpiredFeed(t *testing.T) {
	config := testConfig(t, t.TempDir())
	now := time.Now().UTC()
	manifest := Manifest{
		SchemaVersion: 1, Product: ProductName, ReleaseID: "r1", Version: "1.0.0", Sequence: 1,
		Channel: "stable", PublishedAt: now.Add(-time.Hour), ExpiresAt: now.Add(-time.Second), MinimumUpdaterVersion: "0.1.0",
		Artifacts: map[string]Artifact{ContainerPlatform(): {Repository: "evil.invalid/app", Digest: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", SizeBytes: 1}},
		Database:  DatabasePolicy{FromSchemaMinimum: 1, FromSchemaMaximum: 1, ToSchema: 1},
		Policy:    ReleasePolicy{DrainTimeoutSeconds: 1, StartupTimeoutSeconds: 1, ObservationSeconds: 1, ConsecutiveHealthFailureLimit: 1},
	}
	if err := manifest.Validate(config, now); err == nil {
		t.Fatal("invalid manifest was accepted")
	}
}
