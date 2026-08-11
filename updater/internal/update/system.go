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
	ReleaseID string            `json:"releaseId"`
	CreatedAt time.Time         `json:"createdAt"`
	Files     map[string]string `json:"files"`
}

func (s System) Backup(releaseID string) (string, error) {
	backupDir := filepath.Join(s.Config.StateDir, "backups", releaseID)
	if err := os.MkdirAll(backupDir, 0o700); err != nil {
		return "", err
	}
	database := filepath.Join(s.Config.AIOSDataDir, s.Config.DatabaseRelativePath)
	metadata := backupMetadata{ReleaseID: releaseID, CreatedAt: time.Now().UTC(), Files: map[string]string{}}
	for _, suffix := range []string{"", "-wal"} {
		source := database + suffix
		if _, err := os.Stat(source); os.IsNotExist(err) && suffix != "" {
			continue
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

func (s System) Restore(backupDir string) error {
	metadataData, err := os.ReadFile(filepath.Join(backupDir, "backup.json"))
	if err != nil {
		return err
	}
	var metadata backupMetadata
	if err := json.Unmarshal(metadataData, &metadata); err != nil {
		return err
	}
	database := filepath.Join(s.Config.AIOSDataDir, s.Config.DatabaseRelativePath)
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
