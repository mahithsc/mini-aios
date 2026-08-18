package update

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const maximumCommandOutput = 256 * 1024

const (
	legacyDatabaseRelativePath               = "workspace/aios.db"
	storageLayoutMigrationName               = "storage-layout-v1"
	storageLayoutMigrationReportRelativePath = "state/migrations/storage-layout-v1.json"
	storageLayoutRollbackJournalName         = "storage-layout-v1-rollback.json"
	sessionLayoutMigrationName               = "session-layout-v2"
	sessionLayoutMigrationReportRelativePath = "state/migrations/session-layout-v2.json"
	sessionLayoutRollbackJournalName         = "session-layout-v2-rollback.json"
)

type cappedBuffer struct {
	buffer bytes.Buffer
}

func (c *cappedBuffer) Write(data []byte) (int, error) {
	original := len(data)
	remaining := maximumCommandOutput - c.buffer.Len()
	if remaining > 0 {
		if len(data) > remaining {
			data = data[:remaining]
		}
		_, _ = c.buffer.Write(data)
	}
	return original, nil
}

func (c *cappedBuffer) String() string { return c.buffer.String() }

type System struct {
	Config     Config
	HTTPClient *http.Client
}

func (s System) command(ctx context.Context, executable string, arguments ...string) error {
	command := exec.CommandContext(ctx, executable, arguments...)
	var output cappedBuffer
	command.Stdout = &output
	command.Stderr = &output
	if err := command.Run(); err != nil {
		return fmt.Errorf("%s %s failed: %w: %s", executable, strings.Join(arguments, " "), err, strings.TrimSpace(output.String()))
	}
	return nil
}

func (s System) commandOutput(ctx context.Context, executable string, arguments ...string) (string, error) {
	command := exec.CommandContext(ctx, executable, arguments...)
	var output cappedBuffer
	command.Stdout = &output
	command.Stderr = &output
	if err := command.Run(); err != nil {
		return "", fmt.Errorf("%s failed: %w: %s", executable, err, strings.TrimSpace(output.String()))
	}
	return strings.TrimSpace(output.String()), nil
}

func (s System) composeArguments(arguments ...string) []string {
	base := []string{"compose", "--project-directory", s.Config.ComposeProjectDir, "--env-file", s.Config.ReleaseEnvPath}
	return append(base, arguments...)
}

func (s System) PullAndVerify(ctx context.Context, artifact Artifact) error {
	image := artifact.Image()
	if err := s.command(ctx, s.Config.DockerBinary, "pull", image); err != nil {
		return err
	}
	output, err := s.commandOutput(ctx, s.Config.DockerBinary, "image", "inspect", "--format", "{{json .RepoDigests}}", image)
	if err != nil {
		return err
	}
	if !strings.Contains(output, artifact.Digest) {
		return fmt.Errorf("local Docker image does not report signed digest %s", artifact.Digest)
	}
	return nil
}

func (s System) Stop(ctx context.Context) error {
	return s.command(
		ctx,
		s.Config.DockerBinary,
		s.composeArguments("stop", "--timeout", "30", s.Config.ComposeService)...,
	)
}

func (s System) Start(ctx context.Context) error {
	return s.command(
		ctx,
		s.Config.DockerBinary,
		s.composeArguments("up", "-d", "--no-build", "--no-deps", s.Config.ComposeService)...,
	)
}

func (s System) WriteReleaseEnv(release ReleaseRef) error {
	values := []string{release.Image, release.ReleaseID, release.Version, release.ImageDigest, release.Revision}
	for _, value := range values {
		if strings.ContainsAny(value, "\r\n") {
			return fmt.Errorf("release metadata contains a newline")
		}
	}
	payload := fmt.Sprintf(
		"AIOS_IMAGE=%s\nAIOS_RELEASE_ID=%s\nAIOS_VERSION=%s\nAIOS_RELEASE_SEQUENCE=%d\nAIOS_IMAGE_DIGEST=%s\nAIOS_REVISION=%s\n",
		release.Image,
		release.ReleaseID,
		release.Version,
		release.Sequence,
		release.ImageDigest,
		release.Revision,
	)
	payload += fmt.Sprintf("AIOS_DATABASE_SCHEMA=%d\n", release.DatabaseSchema)
	return writeAtomic(s.Config.ReleaseEnvPath, []byte(payload), 0o600)
}

func (s System) ClearReleaseEnv() error {
	err := os.Remove(s.Config.ReleaseEnvPath)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	directory, openError := os.Open(filepath.Dir(s.Config.ReleaseEnvPath))
	if openError != nil {
		return openError
	}
	defer directory.Close()
	return directory.Sync()
}

func (s System) ReadSelectedRelease() *ReleaseRef {
	data, err := os.ReadFile(s.Config.ReleaseEnvPath)
	if err != nil {
		return nil
	}
	values := map[string]string{}
	for _, line := range strings.Split(string(data), "\n") {
		key, value, ok := strings.Cut(line, "=")
		if ok {
			values[key] = value
		}
	}
	sequence, _ := strconv.ParseInt(values["AIOS_RELEASE_SEQUENCE"], 10, 64)
	databaseSchema, _ := strconv.Atoi(values["AIOS_DATABASE_SCHEMA"])
	if values["AIOS_IMAGE"] == "" {
		return nil
	}
	return &ReleaseRef{
		ReleaseID:      values["AIOS_RELEASE_ID"],
		Version:        values["AIOS_VERSION"],
		Sequence:       sequence,
		Image:          values["AIOS_IMAGE"],
		ImageDigest:    values["AIOS_IMAGE_DIGEST"],
		Revision:       values["AIOS_REVISION"],
		DatabaseSchema: databaseSchema,
	}
}

type drainResponse struct {
	Draining   bool `json:"draining"`
	ActiveRuns int  `json:"activeRuns"`
}

type readyResponse struct {
	Status         string `json:"status"`
	ReleaseID      string `json:"releaseId"`
	Version        string `json:"version"`
	Sequence       int64  `json:"sequence"`
	ImageDigest    string `json:"imageDigest"`
	DatabaseSchema int    `json:"databaseSchema"`
}

func (s System) updaterToken() (string, error) {
	data, err := os.ReadFile(s.Config.UpdaterTokenFile)
	if err != nil {
		return "", fmt.Errorf("read updater token: %w", err)
	}
	token := strings.TrimSpace(string(data))
	if token == "" {
		return "", fmt.Errorf("updater token is empty")
	}
	return token, nil
}

func (s System) callAPI(ctx context.Context, method, path string, target any) error {
	token, err := s.updaterToken()
	if err != nil {
		return err
	}
	request, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(s.Config.HealthURL, "/")+path, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client := s.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 15 * time.Second}
	}
	response, err := client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return fmt.Errorf("updater API %s returned %d: %s", path, response.StatusCode, strings.TrimSpace(string(body)))
	}
	if target == nil {
		return nil
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 1024*1024))
	if err := decoder.Decode(target); err != nil {
		return fmt.Errorf("decode updater API %s: %w", path, err)
	}
	return nil
}

func (s System) Drain(ctx context.Context, timeout time.Duration) error {
	var response drainResponse
	if err := s.callAPI(ctx, http.MethodPost, "/drain", &response); err != nil {
		return err
	}
	deadline := time.Now().Add(timeout)
	for response.ActiveRuns > 0 {
		if time.Now().After(deadline) {
			return fmt.Errorf("drain timed out with %d active runs", response.ActiveRuns)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
		if err := s.callAPI(ctx, http.MethodGet, "/drain", &response); err != nil {
			return err
		}
	}
	return nil
}

func (s System) Resume(ctx context.Context) error {
	deadline := time.Now().Add(15 * time.Second)
	var lastError error
	for {
		lastError = s.callAPI(ctx, http.MethodPost, "/resume", &drainResponse{})
		if lastError == nil {
			return nil
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("resume AIOS after retries: %w", lastError)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func (s System) Ready(ctx context.Context, release ReleaseRef) error {
	var response readyResponse
	if err := s.callAPI(ctx, http.MethodGet, "/ready", &response); err != nil {
		return err
	}
	if response.Status != "ready" {
		return fmt.Errorf("AIOS reports status %q", response.Status)
	}
	if response.ReleaseID != release.ReleaseID || response.Sequence != release.Sequence {
		return fmt.Errorf("AIOS reports release %s/%d, expected %s/%d", response.ReleaseID, response.Sequence, release.ReleaseID, release.Sequence)
	}
	if release.ImageDigest != "" && response.ImageDigest != release.ImageDigest {
		return fmt.Errorf("AIOS reports image digest %q, expected %q", response.ImageDigest, release.ImageDigest)
	}
	if release.DatabaseSchema != 0 && response.DatabaseSchema != release.DatabaseSchema {
		return fmt.Errorf("AIOS reports database schema %d, expected %d", response.DatabaseSchema, release.DatabaseSchema)
	}
	return nil
}

func (s System) WaitReady(ctx context.Context, release ReleaseRef, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	var lastError error
	for time.Now().Before(deadline) {
		if err := s.Ready(ctx, release); err == nil {
			return nil
		} else {
			lastError = err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("AIOS did not become ready: %w", lastError)
}

func (s System) Observe(ctx context.Context, release ReleaseRef, duration time.Duration, failureLimit int) error {
	deadline := time.Now().Add(duration)
	failures := 0
	for time.Now().Before(deadline) {
		if err := s.Ready(ctx, release); err != nil {
			failures++
			if failures >= failureLimit {
				return fmt.Errorf("AIOS failed observation health gate: %w", err)
			}
		} else {
			failures = 0
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(5 * time.Second):
		}
	}
	return nil
}

type backupMetadata struct {
	ReleaseID            string            `json:"releaseId"`
	CreatedAt            time.Time         `json:"createdAt"`
	DatabaseRelativePath string            `json:"databaseRelativePath,omitempty"`
	SessionLayoutVersion int               `json:"sessionLayoutVersion,omitempty"`
	Files                map[string]string `json:"files"`
}

func (s System) Backup(releaseID string) (string, error) {
	backupDir := filepath.Join(s.Config.StateDir, "backups", releaseID)
	if err := os.MkdirAll(backupDir, 0o700); err != nil {
		return "", err
	}
	if err := os.Remove(filepath.Join(backupDir, storageLayoutRollbackJournalName)); err != nil && !os.IsNotExist(err) {
		return "", err
	}
	if err := os.Remove(filepath.Join(backupDir, sessionLayoutRollbackJournalName)); err != nil && !os.IsNotExist(err) {
		return "", err
	}
	databaseRelativePath, err := s.activeDatabaseRelativePath()
	if err != nil {
		return "", err
	}
	sessionLayoutVersion, err := s.sessionLayoutVersion()
	if err != nil {
		return "", err
	}
	database := filepath.Join(s.Config.AIOSDataDir, databaseRelativePath)
	if err := rejectSymlinkParents(s.Config.AIOSDataDir, database); err != nil {
		return "", err
	}
	metadata := backupMetadata{
		ReleaseID:            releaseID,
		CreatedAt:            time.Now().UTC(),
		DatabaseRelativePath: filepath.ToSlash(databaseRelativePath),
		SessionLayoutVersion: sessionLayoutVersion,
		Files:                map[string]string{},
	}
	for _, suffix := range []string{"", "-wal"} {
		source := database + suffix
		info, statErr := os.Lstat(source)
		if os.IsNotExist(statErr) && suffix != "" {
			continue
		}
		if statErr != nil {
			return "", statErr
		}
		if !info.Mode().IsRegular() {
			return "", fmt.Errorf("database backup source is not a regular file: %s", source)
		}
		destination := filepath.Join(backupDir, filepath.Base(source))
		digest, err := copyAndHash(source, destination)
		if err != nil {
			return "", err
		}
		metadata.Files[filepath.Base(source)] = digest
	}
	if len(metadata.Files) == 0 {
		return "", fmt.Errorf("database backup produced no files")
	}
	data, _ := json.MarshalIndent(metadata, "", "  ")
	if err := writeAtomic(filepath.Join(backupDir, "backup.json"), append(data, '\n'), 0o600); err != nil {
		return "", err
	}
	return backupDir, nil
}

// BackupRestoreRequired extends the signed database policy for journaled
// filesystem transitions. A legacy database backup or a completed session
// layout migration must be restored even when the schema is backward-compatible,
// because the previous release also needs its filesystem layout put back.
func (s System) BackupRestoreRequired(backupDir string, policyRequiresRestore bool) (bool, error) {
	if policyRequiresRestore {
		return true, nil
	}
	metadata, err := readBackupMetadata(backupDir)
	if err != nil {
		return false, err
	}
	databaseRelativePath, err := s.backupDatabaseRelativePath(metadata)
	if err != nil {
		return false, err
	}
	if filepath.ToSlash(databaseRelativePath) == legacyDatabaseRelativePath {
		return true, nil
	}
	if metadata.SessionLayoutVersion >= 2 {
		return false, nil
	}
	_, err = os.Stat(filepath.Join(
		s.Config.AIOSDataDir,
		filepath.FromSlash(sessionLayoutMigrationReportRelativePath),
	))
	if err == nil {
		return true, nil
	}
	if !os.IsNotExist(err) {
		return false, err
	}
	return policyRequiresRestore, nil
}

func (s System) Restore(backupDir string) error {
	metadata, err := readBackupMetadata(backupDir)
	if err != nil {
		return err
	}
	databaseRelativePath, err := s.backupDatabaseRelativePath(metadata)
	if err != nil {
		return err
	}
	databaseBase := filepath.Base(databaseRelativePath)
	if err := verifyBackupFiles(backupDir, databaseBase, metadata.Files); err != nil {
		return err
	}
	if metadata.SessionLayoutVersion < 2 {
		if err := s.reverseSessionLayoutMigration(backupDir); err != nil {
			return fmt.Errorf("reverse session layout migration: %w", err)
		}
	}
	if filepath.ToSlash(databaseRelativePath) == legacyDatabaseRelativePath {
		if err := s.reverseStorageLayoutMigration(backupDir); err != nil {
			return fmt.Errorf("reverse storage layout migration: %w", err)
		}
	}
	database := filepath.Join(s.Config.AIOSDataDir, databaseRelativePath)
	if err := rejectSymlinkParents(s.Config.AIOSDataDir, database); err != nil {
		return err
	}
	for _, suffix := range []string{"", "-wal", "-shm"} {
		_ = os.Remove(database + suffix)
	}
	for name, expectedDigest := range metadata.Files {
		source := filepath.Join(backupDir, filepath.Base(name))
		destination := filepath.Join(filepath.Dir(database), filepath.Base(name))
		digest, err := copyAndHash(source, destination)
		if err != nil {
			return err
		}
		if digest != expectedDigest {
			return fmt.Errorf("restored file %s failed hash verification", name)
		}
	}
	return nil
}

func cleanDatabaseRelativePath(path string) (string, error) {
	cleaned := filepath.Clean(filepath.FromSlash(path))
	if cleaned == "." || filepath.IsAbs(cleaned) || cleaned == ".." || strings.HasPrefix(cleaned, ".."+string(os.PathSeparator)) {
		return "", fmt.Errorf("must stay beneath aios_data_dir: %q", path)
	}
	return cleaned, nil
}

func (s System) activeDatabaseRelativePath() (string, error) {
	configured, err := cleanDatabaseRelativePath(s.Config.DatabaseRelativePath)
	if err != nil {
		return "", err
	}
	canonical := filepath.FromSlash(canonicalDatabaseRelativePath)
	legacy := filepath.FromSlash(legacyDatabaseRelativePath)
	candidates := []string{configured, legacy}
	if configured == canonical {
		// workspace/aios.db was the active database in the release immediately
		// before the canonical layout. It wins when both legacy workspace and
		// stale state databases exist, matching the application migration.
		candidates = []string{legacy, configured}
	}
	seen := map[string]bool{}
	checked := make([]string, 0, len(candidates))
	for _, relativePath := range candidates {
		if seen[relativePath] {
			continue
		}
		seen[relativePath] = true
		checked = append(checked, filepath.ToSlash(relativePath))
		databasePath := filepath.Join(s.Config.AIOSDataDir, relativePath)
		if err := rejectSymlinkParents(s.Config.AIOSDataDir, databasePath); err != nil {
			return "", err
		}
		info, statErr := os.Lstat(databasePath)
		if os.IsNotExist(statErr) {
			continue
		}
		if statErr != nil {
			return "", statErr
		}
		if !info.Mode().IsRegular() {
			return "", fmt.Errorf("database path %s is not a regular file", filepath.ToSlash(relativePath))
		}
		return relativePath, nil
	}
	return "", fmt.Errorf("database not found beneath aios_data_dir (checked %s)", strings.Join(checked, ", "))
}

func readBackupMetadata(backupDir string) (backupMetadata, error) {
	metadataData, err := os.ReadFile(filepath.Join(backupDir, "backup.json"))
	if err != nil {
		return backupMetadata{}, err
	}
	var metadata backupMetadata
	if err := json.Unmarshal(metadataData, &metadata); err != nil {
		return backupMetadata{}, err
	}
	return metadata, nil
}

func (s System) sessionLayoutVersion() (int, error) {
	reportPath := filepath.Join(
		s.Config.AIOSDataDir,
		filepath.FromSlash(sessionLayoutMigrationReportRelativePath),
	)
	reportData, err := os.ReadFile(reportPath)
	if os.IsNotExist(err) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	var report storageLayoutMigrationReport
	if err := json.Unmarshal(reportData, &report); err != nil {
		return 0, fmt.Errorf("decode %s: %w", sessionLayoutMigrationName, err)
	}
	if report.Version != 2 || report.Migration != sessionLayoutMigrationName || (report.Status != "in_progress" && report.Status != "complete") {
		return 0, fmt.Errorf("unsupported session layout migration report")
	}
	if !filepath.IsAbs(report.DataRoot) {
		return 0, fmt.Errorf("session layout migration data root is not absolute")
	}
	return report.Version, nil
}

func verifyBackupFiles(backupDir, databaseBase string, files map[string]string) error {
	if len(files) == 0 {
		return fmt.Errorf("database backup metadata contains no files")
	}
	for name, expectedDigest := range files {
		if name != databaseBase && name != databaseBase+"-wal" {
			return fmt.Errorf("backup contains unexpected database file %q", name)
		}
		digest, err := hashFile(filepath.Join(backupDir, name))
		if err != nil {
			return err
		}
		if digest != expectedDigest {
			return fmt.Errorf("backup file %s failed hash verification", name)
		}
	}
	return nil
}

func hashFile(path string) (string, error) {
	input, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer input.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, input); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func (s System) backupDatabaseRelativePath(metadata backupMetadata) (string, error) {
	relativePath := metadata.DatabaseRelativePath
	if relativePath == "" {
		// Backups made by an older updater did not include the source path and
		// always restore to the configured location for backward compatibility.
		relativePath = s.Config.DatabaseRelativePath
	}
	return cleanDatabaseRelativePath(relativePath)
}

type storageLayoutMigrationReport struct {
	Version   int                            `json:"version"`
	Migration string                         `json:"migration"`
	DataRoot  string                         `json:"dataRoot"`
	Status    string                         `json:"status"`
	Actions   []storageLayoutMigrationAction `json:"actions"`
}

type storageLayoutMigrationAction struct {
	Action      string `json:"action"`
	Source      string `json:"source"`
	Destination string `json:"destination"`
	Status      string `json:"status,omitempty"`
	Project     string `json:"project,omitempty"`
	Registry    string `json:"registry,omitempty"`
}

type storageLayoutRollbackJournal struct {
	FormatVersion int    `json:"formatVersion"`
	ReportSHA256  string `json:"reportSha256"`
	NextAction    int    `json:"nextAction"`
}

func (s System) reverseStorageLayoutMigration(backupDir string) error {
	return s.reverseLayoutMigration(
		backupDir,
		storageLayoutMigrationName,
		storageLayoutMigrationReportRelativePath,
		storageLayoutRollbackJournalName,
		1,
	)
}

func (s System) reverseSessionLayoutMigration(backupDir string) error {
	return s.reverseLayoutMigration(
		backupDir,
		sessionLayoutMigrationName,
		sessionLayoutMigrationReportRelativePath,
		sessionLayoutRollbackJournalName,
		2,
	)
}

func (s System) reverseLayoutMigration(
	backupDir string,
	migrationName string,
	reportRelativePath string,
	journalName string,
	expectedVersion int,
) error {
	reportPath := filepath.Join(s.Config.AIOSDataDir, filepath.FromSlash(reportRelativePath))
	journalPath := filepath.Join(backupDir, journalName)
	reportData, err := os.ReadFile(reportPath)
	if os.IsNotExist(err) {
		if removeErr := os.Remove(journalPath); removeErr != nil && !os.IsNotExist(removeErr) {
			return removeErr
		}
		return nil
	}
	if err != nil {
		return err
	}
	var report storageLayoutMigrationReport
	if err := json.Unmarshal(reportData, &report); err != nil {
		return fmt.Errorf("decode %s: %w", migrationName, err)
	}
	if report.Version != expectedVersion || report.Migration != migrationName || (report.Status != "in_progress" && report.Status != "complete") {
		return fmt.Errorf("unsupported storage migration report")
	}
	if !filepath.IsAbs(report.DataRoot) {
		return fmt.Errorf("storage migration data root is not absolute")
	}

	reportDigest := sha256.Sum256(reportData)
	reportSHA256 := hex.EncodeToString(reportDigest[:])
	journal, err := loadStorageLayoutRollbackJournal(journalPath)
	if os.IsNotExist(err) {
		journal = storageLayoutRollbackJournal{
			FormatVersion: 1,
			ReportSHA256:  reportSHA256,
			NextAction:    len(report.Actions) - 1,
		}
		if err := saveStorageLayoutRollbackJournal(journalPath, journal); err != nil {
			return err
		}
	} else if err != nil {
		return err
	} else if journal.FormatVersion != 1 || journal.ReportSHA256 != reportSHA256 || journal.NextAction < -1 || journal.NextAction >= len(report.Actions) {
		return fmt.Errorf("storage layout rollback journal does not match migration report")
	}

	for journal.NextAction >= 0 {
		if err := s.reverseStorageLayoutAction(report, journal.NextAction); err != nil {
			return err
		}
		journal.NextAction--
		if err := saveStorageLayoutRollbackJournal(journalPath, journal); err != nil {
			return err
		}
	}

	if err := os.Remove(reportPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	if err := syncDirectory(filepath.Dir(reportPath)); err != nil {
		return err
	}
	if err := os.Remove(journalPath); err != nil && !os.IsNotExist(err) {
		return err
	}
	return syncDirectory(filepath.Dir(journalPath))
}

func (s System) reverseStorageLayoutAction(report storageLayoutMigrationReport, index int) error {
	action := report.Actions[index]
	if action.Status != "" && action.Status != "planned" && action.Status != "complete" {
		return fmt.Errorf("unsupported storage migration action status %q", action.Status)
	}
	switch action.Action {
	case "rewrote-deployment-source":
		if err := s.reverseDeploymentSourceRewrite(report.DataRoot, action); err != nil {
			return err
		}
		return nil
	case "moved", "archived", "promoted-database", "promoted-database-snapshot":
	default:
		return fmt.Errorf("unsupported storage migration action %q", action.Action)
	}
	sourceRelative, err := migrationRelativePath(report.DataRoot, action.Source)
	if err != nil {
		return fmt.Errorf("invalid migration source: %w", err)
	}
	destinationRelative, err := migrationRelativePath(report.DataRoot, action.Destination)
	if err != nil {
		return fmt.Errorf("invalid migration destination: %w", err)
	}
	source := filepath.Join(s.Config.AIOSDataDir, sourceRelative)
	destination := filepath.Join(s.Config.AIOSDataDir, destinationRelative)
	var reverseErr error
	if action.Action == "promoted-database-snapshot" {
		reverseErr = reverseDatabaseSnapshot(s.Config.AIOSDataDir, source, destination)
	} else {
		reverseErr = reverseMigrationMove(s.Config.AIOSDataDir, source, destination)
	}
	if reverseErr != nil {
		return fmt.Errorf("reverse %s from %s to %s: %w", action.Action, filepath.ToSlash(destinationRelative), filepath.ToSlash(sourceRelative), reverseErr)
	}
	return nil
}

func loadStorageLayoutRollbackJournal(path string) (storageLayoutRollbackJournal, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return storageLayoutRollbackJournal{}, err
	}
	var journal storageLayoutRollbackJournal
	if err := json.Unmarshal(data, &journal); err != nil {
		return storageLayoutRollbackJournal{}, err
	}
	return journal, nil
}

func saveStorageLayoutRollbackJournal(path string, journal storageLayoutRollbackJournal) error {
	data, err := json.MarshalIndent(journal, "", "  ")
	if err != nil {
		return err
	}
	return writeAtomic(path, append(data, '\n'), 0o600)
}

func (s System) reverseDeploymentSourceRewrite(reportRoot string, action storageLayoutMigrationAction) error {
	if action.Project == "" || action.Registry == "" {
		return fmt.Errorf("rewritten deployment source action is missing project or registry")
	}
	registryRelative, err := migrationRelativePath(reportRoot, action.Registry)
	if err != nil {
		return fmt.Errorf("invalid deployment registry path: %w", err)
	}
	registryPath := filepath.Join(s.Config.AIOSDataDir, registryRelative)
	if err := rejectSymlinkParents(s.Config.AIOSDataDir, registryPath); err != nil {
		return err
	}
	registryData, err := os.ReadFile(registryPath)
	if err != nil {
		return err
	}
	var registry map[string]any
	if err := json.Unmarshal(registryData, &registry); err != nil {
		return fmt.Errorf("decode deployment registry: %w", err)
	}
	rawProject, exists := registry[action.Project]
	if !exists {
		return fmt.Errorf("deployment registry is missing project %q", action.Project)
	}
	project, ok := rawProject.(map[string]any)
	if !ok {
		return fmt.Errorf("deployment registry project %q is invalid", action.Project)
	}
	currentSource, ok := project["source_dir"].(string)
	if !ok {
		return fmt.Errorf("deployment registry project %q has no source_dir", action.Project)
	}
	if currentSource == action.Source {
		return nil
	}
	if currentSource != action.Destination {
		return fmt.Errorf("deployment registry project %q source changed during migration", action.Project)
	}
	project["source_dir"] = action.Source
	updated, err := json.MarshalIndent(registry, "", "  ")
	if err != nil {
		return err
	}
	return writeAtomic(registryPath, append(updated, '\n'), 0o600)
}

func migrationRelativePath(reportRoot, reportedPath string) (string, error) {
	if !filepath.IsAbs(reportedPath) {
		return "", fmt.Errorf("path is not absolute: %q", reportedPath)
	}
	relativePath, err := filepath.Rel(filepath.Clean(reportRoot), filepath.Clean(reportedPath))
	if err != nil {
		return "", err
	}
	if relativePath == "." || relativePath == ".." || strings.HasPrefix(relativePath, ".."+string(os.PathSeparator)) || filepath.IsAbs(relativePath) {
		return "", fmt.Errorf("path escapes reported data root: %q", reportedPath)
	}
	return relativePath, nil
}

func reverseMigrationMove(dataRoot, source, destination string) error {
	sourceExists, err := pathExists(source)
	if err != nil {
		return err
	}
	destinationExists, err := pathExists(destination)
	if err != nil {
		return err
	}
	if sourceExists && !destinationExists {
		return nil
	}
	if sourceExists && destinationExists {
		return fmt.Errorf("both original and migrated paths exist")
	}
	if !destinationExists {
		return fmt.Errorf("neither original nor migrated path exists")
	}
	if err := rejectSymlinkParents(dataRoot, source); err != nil {
		return err
	}
	if err := rejectSymlinkParents(dataRoot, destination); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(source), 0o700); err != nil {
		return err
	}
	if err := rejectSymlinkParents(dataRoot, source); err != nil {
		return err
	}
	if err := os.Rename(destination, source); err != nil {
		return err
	}
	if err := syncDirectory(filepath.Dir(source)); err != nil {
		return err
	}
	if filepath.Dir(source) != filepath.Dir(destination) {
		return syncDirectory(filepath.Dir(destination))
	}
	return nil
}

func reverseDatabaseSnapshot(dataRoot, source, destination string) error {
	sourceExists, err := pathExists(source)
	if err != nil {
		return err
	}
	destinationExists, err := pathExists(destination)
	if err != nil {
		return err
	}
	if sourceExists && !destinationExists {
		return nil
	}
	if !sourceExists {
		return fmt.Errorf("original database is unavailable")
	}
	if err := rejectSymlinkParents(dataRoot, destination); err != nil {
		return err
	}
	if err := os.Remove(destination); err != nil {
		return err
	}
	return syncDirectory(filepath.Dir(destination))
}

func pathExists(path string) (bool, error) {
	_, err := os.Lstat(path)
	if os.IsNotExist(err) {
		return false, nil
	}
	return err == nil, err
}

func rejectSymlinkParents(dataRoot, path string) error {
	relativePath, err := filepath.Rel(filepath.Clean(dataRoot), filepath.Clean(path))
	if err != nil || relativePath == ".." || strings.HasPrefix(relativePath, ".."+string(os.PathSeparator)) || filepath.IsAbs(relativePath) {
		return fmt.Errorf("path escapes aios_data_dir: %q", path)
	}
	current := filepath.Clean(dataRoot)
	parts := strings.Split(filepath.Dir(relativePath), string(os.PathSeparator))
	for _, part := range parts {
		if part == "." || part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, statErr := os.Lstat(current)
		if os.IsNotExist(statErr) {
			break
		}
		if statErr != nil {
			return statErr
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("path parent is a symbolic link: %s", current)
		}
		if !info.IsDir() {
			return fmt.Errorf("path parent is not a directory: %s", current)
		}
	}
	return nil
}

func syncDirectory(path string) error {
	directory, err := os.Open(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	defer directory.Close()
	return directory.Sync()
}

func copyAndHash(source, destination string) (string, error) {
	input, err := os.Open(source)
	if err != nil {
		return "", err
	}
	defer input.Close()
	if err := os.MkdirAll(filepath.Dir(destination), 0o700); err != nil {
		return "", err
	}
	output, err := os.OpenFile(destination, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o600)
	if err != nil {
		return "", err
	}
	hash := sha256.New()
	_, copyErr := io.Copy(io.MultiWriter(output, hash), input)
	syncErr := output.Sync()
	closeErr := output.Close()
	if copyErr != nil {
		return "", copyErr
	}
	if syncErr != nil {
		return "", syncErr
	}
	if closeErr != nil {
		return "", closeErr
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func (s System) FreeBytes() (uint64, error) {
	var stats syscall.Statfs_t
	if err := syscall.Statfs(s.Config.StateDir, &stats); err != nil {
		return 0, err
	}
	return uint64(stats.Bavail) * uint64(stats.Bsize), nil
}
