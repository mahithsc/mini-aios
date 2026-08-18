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

func TestConfigRejectsDatabasePathOutsideDataRoot(t *testing.T) {
	config := testConfig(t, t.TempDir())
	config.DatabaseRelativePath = filepath.Join("..", "outside.db")
	if err := config.Validate(); err == nil {
		t.Fatal("config accepted a database path outside aios_data_dir")
	}
}
