package update

import (
	"path/filepath"
	"testing"
)

func TestConfigDefaultsDatabaseToCanonicalState(t *testing.T) {
	var config Config
	config.applyDefaults()

	want := filepath.Join("state", "aios.db")
	if config.DatabaseRelativePath != want {
		t.Fatalf(
			"default database path = %q, want %q",
			config.DatabaseRelativePath,
			want,
		)
	}
}
