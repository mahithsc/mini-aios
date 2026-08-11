package update

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

type ReleaseRef struct {
	ReleaseID      string `json:"releaseId"`
	Version        string `json:"version"`
	Sequence       int64  `json:"sequence"`
	Image          string `json:"image"`
	ImageDigest    string `json:"imageDigest"`
	Revision       string `json:"revision,omitempty"`
	DatabaseSchema int    `json:"databaseSchema"`
}

func ReleaseFromManifest(manifest Manifest, artifact Artifact) ReleaseRef {
	return ReleaseRef{
		ReleaseID:      manifest.ReleaseID,
		Version:        manifest.Version,
		Sequence:       manifest.Sequence,
		Image:          artifact.Image(),
		ImageDigest:    artifact.Digest,
		Revision:       manifest.Revision,
		DatabaseSchema: manifest.Database.ToSchema,
	}
}

type State struct {
	FormatVersion           int         `json:"formatVersion"`
	Status                  string      `json:"status"`
	Attempt                 int         `json:"attempt"`
	Current                 *ReleaseRef `json:"current,omitempty"`
	Previous                *ReleaseRef `json:"previous,omitempty"`
	Target                  *ReleaseRef `json:"target,omitempty"`
	BackupDir               string      `json:"backupDir,omitempty"`
	RestoreBackupOnRollback bool        `json:"restoreBackupOnRollback,omitempty"`
	LastFailedSequence      int64       `json:"lastFailedSequence,omitempty"`
	LastError               string      `json:"lastError,omitempty"`
	Bootstrap               bool        `json:"bootstrap,omitempty"`
	Transitioned            time.Time   `json:"transitionedAt"`
}

func InitialState() State {
	return State{FormatVersion: 1, Status: "idle", Transitioned: time.Now().UTC()}
}

type StateStore struct {
	Path string
}

func (s StateStore) Load() (State, error) {
	data, err := os.ReadFile(s.Path)
	if os.IsNotExist(err) {
		return InitialState(), nil
	}
	if err != nil {
		return State{}, err
	}
	var state State
	if err := json.Unmarshal(data, &state); err != nil {
		return State{}, fmt.Errorf("decode updater state: %w", err)
	}
	if state.FormatVersion != 1 {
		return State{}, fmt.Errorf("unsupported updater state format %d", state.FormatVersion)
	}
	return state, nil
}

func (s StateStore) Save(state State) error {
	state.FormatVersion = 1
	state.Transitioned = time.Now().UTC()
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return writeAtomic(s.Path, data, 0o600)
}

func writeAtomic(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".atomic-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if err := temporary.Chmod(mode); err != nil {
		temporary.Close()
		return err
	}
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	if err := os.Rename(temporaryName, path); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(path))
	if err == nil {
		defer directory.Close()
		_ = directory.Sync()
	}
	return nil
}
