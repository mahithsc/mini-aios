package update

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

func TestResumeRetriesTransientFailure(t *testing.T) {
	var attempts atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/resume" {
			t.Fatalf("unexpected path %s", request.URL.Path)
		}
		if request.Header.Get("Authorization") != "Bearer test-token" {
			t.Fatalf("missing updater authorization")
		}
		if attempts.Add(1) == 1 {
			http.Error(writer, "restarting", http.StatusServiceUnavailable)
			return
		}
		writer.Header().Set("Content-Type", "application/json")
		_, _ = writer.Write([]byte(`{"draining":false,"activeRuns":0}`))
	}))
	defer server.Close()

	tokenPath := filepath.Join(t.TempDir(), "updater-token")
	if err := os.WriteFile(tokenPath, []byte("test-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	system := System{Config: Config{
		HealthURL:        server.URL,
		UpdaterTokenFile: tokenPath,
	}}
	if err := system.Resume(context.Background()); err != nil {
		t.Fatal(err)
	}
	if attempts.Load() != 2 {
		t.Fatalf("got %d attempts, want 2", attempts.Load())
	}
}
