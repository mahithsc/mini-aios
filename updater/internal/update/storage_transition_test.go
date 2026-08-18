package update

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestBackupPrefersAndRecordsActiveLegacyDatabase(t *testing.T) {
	root := t.TempDir()
	system := transitionTestSystem(root)
	writeTestFile(t, filepath.Join(system.Config.AIOSDataDir, "workspace", "aios.db"), "active legacy database")
	writeTestFile(t, filepath.Join(system.Config.AIOSDataDir, "workspace", "aios.db-wal"), "active legacy wal")
	writeTestFile(t, filepath.Join(system.Config.AIOSDataDir, "state", "aios.db"), "stale state database")

	backupDir, err := system.Backup("release-2")
	if err != nil {
		t.Fatal(err)
	}
	metadata, err := readBackupMetadata(backupDir)
	if err != nil {
		t.Fatal(err)
	}
	if metadata.DatabaseRelativePath != legacyDatabaseRelativePath {
		t.Fatalf("databaseRelativePath = %q, want %q", metadata.DatabaseRelativePath, legacyDatabaseRelativePath)
	}
	backupDatabase, err := os.ReadFile(filepath.Join(backupDir, "aios.db"))
	if err != nil {
		t.Fatal(err)
	}
	if string(backupDatabase) != "active legacy database" {
		t.Fatalf("backed up database = %q", backupDatabase)
	}
	required, err := system.BackupRestoreRequired(backupDir, false)
	if err != nil {
		t.Fatal(err)
	}
	if !required {
		t.Fatal("legacy storage transition did not require a rollback restore")
	}
}

func TestCanonicalBackupHonorsNoRestorePolicy(t *testing.T) {
	root := t.TempDir()
	system := transitionTestSystem(root)
	writeTestFile(t, filepath.Join(system.Config.AIOSDataDir, "state", "aios.db"), "canonical database")

	backupDir, err := system.Backup("release-3")
	if err != nil {
		t.Fatal(err)
	}
	required, err := system.BackupRestoreRequired(backupDir, false)
	if err != nil {
		t.Fatal(err)
	}
	if required {
		t.Fatal("canonical database backup overrode a no-restore release policy")
	}
}

func TestRestoreReturnsDatabaseAndFilesystemToLegacyLayout(t *testing.T) {
	root := t.TempDir()
	system := transitionTestSystem(root)
	dataRoot := system.Config.AIOSDataDir
	containerRoot := "/root/.mini-aios"
	legacyDatabase := filepath.Join(dataRoot, "workspace", "aios.db")
	canonicalDatabase := filepath.Join(dataRoot, "state", "aios.db")
	legacyScratch := filepath.Join(dataRoot, "workspace", "session", "chat-1", "files")
	canonicalScratch := filepath.Join(dataRoot, "sessions", "chat-1", "scratch")
	archivedStateDatabase := filepath.Join(dataRoot, "legacy", storageLayoutMigrationName, "state", "aios.db")
	archivedWorkspaceDatabase := filepath.Join(dataRoot, "legacy", storageLayoutMigrationName, "workspace", "aios.db")
	legacyDeployments := filepath.Join(dataRoot, "workspace", "deploy")
	canonicalDeployments := filepath.Join(dataRoot, "deployments")
	legacyProjectSource := filepath.Join(containerRoot, "workspace", "session", "chat-1", "files", "project")
	canonicalProjectSource := filepath.Join(containerRoot, "sessions", "chat-1", "scratch", "project")

	writeTestFile(t, legacyDatabase, "pre-update active database")
	writeTestFile(t, canonicalDatabase, "pre-update stale state database")
	writeTestFile(t, filepath.Join(legacyScratch, "draft.txt"), "draft")
	writeTestFile(
		t,
		filepath.Join(legacyDeployments, "projects.json"),
		`{"demo":{"source_dir":"`+legacyProjectSource+`"}}`,
	)
	backupDir, err := system.Backup("release-2")
	if err != nil {
		t.Fatal(err)
	}

	if err := os.MkdirAll(filepath.Dir(archivedStateDatabase), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(canonicalDatabase, archivedStateDatabase); err != nil {
		t.Fatal(err)
	}
	legacyDatabaseContents, err := os.ReadFile(legacyDatabase)
	if err != nil {
		t.Fatal(err)
	}
	writeTestFile(t, canonicalDatabase, string(legacyDatabaseContents))
	if err := os.MkdirAll(filepath.Dir(archivedWorkspaceDatabase), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(legacyDatabase, archivedWorkspaceDatabase); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(canonicalScratch), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(legacyScratch, canonicalScratch); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(legacyDeployments, canonicalDeployments); err != nil {
		t.Fatal(err)
	}
	writeTestFile(
		t,
		filepath.Join(canonicalDeployments, "projects.json"),
		`{"demo":{"source_dir":"`+canonicalProjectSource+`"}}`,
	)
	writeTestFile(t, canonicalDatabase, "candidate-mutated database")

	report := storageLayoutMigrationReport{
		Version:   1,
		Migration: storageLayoutMigrationName,
		DataRoot:  containerRoot,
		Status:    "in_progress",
		Actions: []storageLayoutMigrationAction{
			{
				Action:      "archived",
				Source:      filepath.Join(containerRoot, "state", "aios.db"),
				Destination: filepath.Join(containerRoot, "legacy", storageLayoutMigrationName, "state", "aios.db"),
			},
			{
				Action:      "promoted-database-snapshot",
				Source:      filepath.Join(containerRoot, "workspace", "aios.db"),
				Destination: filepath.Join(containerRoot, "state", "aios.db"),
			},
			{
				Action:      "archived",
				Source:      filepath.Join(containerRoot, "workspace", "aios.db"),
				Destination: filepath.Join(containerRoot, "legacy", storageLayoutMigrationName, "workspace", "aios.db"),
			},
			{
				Action:      "moved",
				Source:      filepath.Join(containerRoot, "workspace", "session", "chat-1", "files"),
				Destination: filepath.Join(containerRoot, "sessions", "chat-1", "scratch"),
			},
			{
				Action:      "moved",
				Source:      filepath.Join(containerRoot, "workspace", "deploy"),
				Destination: filepath.Join(containerRoot, "deployments"),
			},
			{
				Action:      "rewrote-deployment-source",
				Source:      legacyProjectSource,
				Destination: canonicalProjectSource,
				Status:      "planned",
				Project:     "demo",
				Registry:    filepath.Join(containerRoot, "deployments", "projects.json"),
			},
		},
	}
	reportData, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	reportPath := filepath.Join(dataRoot, filepath.FromSlash(storageLayoutMigrationReportRelativePath))
	writeTestFile(t, reportPath, string(reportData))

	if err := system.Restore(backupDir); err != nil {
		t.Fatal(err)
	}
	assertTestFile(t, legacyDatabase, "pre-update active database")
	assertTestFile(t, canonicalDatabase, "pre-update stale state database")
	assertTestFile(t, filepath.Join(legacyScratch, "draft.txt"), "draft")
	registryData, err := os.ReadFile(filepath.Join(legacyDeployments, "projects.json"))
	if err != nil {
		t.Fatal(err)
	}
	var registry map[string]map[string]string
	if err := json.Unmarshal(registryData, &registry); err != nil {
		t.Fatal(err)
	}
	if registry["demo"]["source_dir"] != legacyProjectSource {
		t.Fatalf("deployment source = %q, want %q", registry["demo"]["source_dir"], legacyProjectSource)
	}
	if _, err := os.Stat(canonicalScratch); !os.IsNotExist(err) {
		t.Fatalf("canonical scratch remains after rollback: %v", err)
	}
	if _, err := os.Stat(reportPath); !os.IsNotExist(err) {
		t.Fatalf("migration report remains after rollback: %v", err)
	}

	// Database restoration remains safe to retry after an interrupted rollback;
	// the migration marker has already been removed and the legacy paths exist.
	if err := system.Restore(backupDir); err != nil {
		t.Fatalf("second restore failed: %v", err)
	}
}

func TestRestoreRejectsMigrationActionOutsideReportedDataRoot(t *testing.T) {
	root := t.TempDir()
	system := transitionTestSystem(root)
	writeTestFile(t, filepath.Join(system.Config.AIOSDataDir, "workspace", "aios.db"), "active legacy database")
	backupDir, err := system.Backup("release-2")
	if err != nil {
		t.Fatal(err)
	}

	report := storageLayoutMigrationReport{
		Version:   1,
		Migration: storageLayoutMigrationName,
		DataRoot:  "/root/.mini-aios",
		Status:    "complete",
		Actions: []storageLayoutMigrationAction{{
			Action:      "moved",
			Source:      "/root/.mini-aios/workspace/aios.db",
			Destination: "/etc/passwd",
		}},
	}
	reportData, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	writeTestFile(
		t,
		filepath.Join(system.Config.AIOSDataDir, filepath.FromSlash(storageLayoutMigrationReportRelativePath)),
		string(reportData),
	)
	if err := system.Restore(backupDir); err == nil {
		t.Fatal("restore accepted a migration action outside the reported data root")
	}
}

func TestRestoreVerifiesBackupBeforeChangingLiveData(t *testing.T) {
	root := t.TempDir()
	system := transitionTestSystem(root)
	legacyDatabase := filepath.Join(system.Config.AIOSDataDir, "workspace", "aios.db")
	writeTestFile(t, legacyDatabase, "live database")
	backupDir, err := system.Backup("release-2")
	if err != nil {
		t.Fatal(err)
	}
	writeTestFile(t, filepath.Join(backupDir, "aios.db"), "corrupt backup")

	if err := system.Restore(backupDir); err == nil {
		t.Fatal("restore accepted a corrupt backup")
	}
	assertTestFile(t, legacyDatabase, "live database")
}

func TestRestoreSupportsLegacyPromotedDatabaseReport(t *testing.T) {
	root := t.TempDir()
	system := transitionTestSystem(root)
	legacyDatabase := filepath.Join(system.Config.AIOSDataDir, "workspace", "aios.db")
	canonicalDatabase := filepath.Join(system.Config.AIOSDataDir, "state", "aios.db")
	writeTestFile(t, legacyDatabase, "pre-update database")
	backupDir, err := system.Backup("release-2")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(canonicalDatabase), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(legacyDatabase, canonicalDatabase); err != nil {
		t.Fatal(err)
	}
	writeTestFile(t, canonicalDatabase, "candidate-mutated database")

	containerRoot := "/root/.mini-aios"
	report := storageLayoutMigrationReport{
		Version:   1,
		Migration: storageLayoutMigrationName,
		DataRoot:  containerRoot,
		Status:    "complete",
		Actions: []storageLayoutMigrationAction{{
			Action:      "promoted-database",
			Source:      filepath.Join(containerRoot, "workspace", "aios.db"),
			Destination: filepath.Join(containerRoot, "state", "aios.db"),
		}},
	}
	reportData, err := json.Marshal(report)
	if err != nil {
		t.Fatal(err)
	}
	writeTestFile(
		t,
		filepath.Join(system.Config.AIOSDataDir, filepath.FromSlash(storageLayoutMigrationReportRelativePath)),
		string(reportData),
	)
	// Simulate a power loss after the move was reversed but before the updater
	// advanced its root-owned rollback journal. Retrying the same action must be
	// harmless and must not replay later completed actions.
	reportDigest := sha256.Sum256(reportData)
	journal := storageLayoutRollbackJournal{
		FormatVersion: 1,
		ReportSHA256:  hex.EncodeToString(reportDigest[:]),
		NextAction:    0,
	}
	if err := saveStorageLayoutRollbackJournal(
		filepath.Join(backupDir, storageLayoutRollbackJournalName),
		journal,
	); err != nil {
		t.Fatal(err)
	}
	if err := system.reverseStorageLayoutAction(report, 0); err != nil {
		t.Fatal(err)
	}

	if err := system.Restore(backupDir); err != nil {
		t.Fatal(err)
	}
	assertTestFile(t, legacyDatabase, "pre-update database")
	if _, err := os.Stat(canonicalDatabase); !os.IsNotExist(err) {
		t.Fatalf("canonical database remains after legacy rollback: %v", err)
	}
}

func transitionTestSystem(root string) System {
	return System{Config: Config{
		AIOSDataDir:          filepath.Join(root, "data"),
		DatabaseRelativePath: filepath.FromSlash(canonicalDatabaseRelativePath),
		StateDir:             filepath.Join(root, "updater-state"),
	}}
}

func writeTestFile(t *testing.T, path, contents string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(contents), 0o600); err != nil {
		t.Fatal(err)
	}
}

func assertTestFile(t *testing.T, path, expected string) {
	t.Helper()
	contents, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(contents) != expected {
		t.Fatalf("%s = %q, want %q", path, contents, expected)
	}
}
