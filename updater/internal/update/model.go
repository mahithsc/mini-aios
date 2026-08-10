package update

import (
	"fmt"
	"runtime"
	"strings"
	"time"
)

const (
	FeedFormatVersion = 1
	ProductName       = "mini-aios"
)

type Envelope struct {
	FormatVersion int    `json:"formatVersion"`
	KeyID         string `json:"keyId"`
	Payload       string `json:"payload"`
	Signature     string `json:"signature"`
}

type Artifact struct {
	Repository string `json:"repository"`
	Digest     string `json:"digest"`
	SizeBytes  int64  `json:"sizeBytes"`
}

func (a Artifact) Image() string {
	return a.Repository + "@" + a.Digest
}

type DatabasePolicy struct {
	FromSchemaMinimum          int  `json:"fromSchemaMinimum"`
	FromSchemaMaximum          int  `json:"fromSchemaMaximum"`
	ToSchema                   int  `json:"toSchema"`
	PreviousAppCanReadToSchema bool `json:"previousAppCanReadToSchema"`
	RestoreBackupOnRollback    bool `json:"restoreBackupOnRollback"`
	Destructive                bool `json:"destructive"`
}

type ReleasePolicy struct {
	Critical                      bool `json:"critical"`
	AllowForcedDrain              bool `json:"allowForcedDrain"`
	DrainTimeoutSeconds           int  `json:"drainTimeoutSeconds"`
	StartupTimeoutSeconds         int  `json:"startupTimeoutSeconds"`
	ObservationSeconds            int  `json:"observationSeconds"`
	ConsecutiveHealthFailureLimit int  `json:"consecutiveHealthFailureLimit"`
}

type Manifest struct {
	SchemaVersion         int                 `json:"schemaVersion"`
	Product               string              `json:"product"`
	ReleaseID             string              `json:"releaseId"`
	Version               string              `json:"version"`
	Sequence              int64               `json:"sequence"`
	Channel               string              `json:"channel"`
	PublishedAt           time.Time           `json:"publishedAt"`
	ExpiresAt             time.Time           `json:"expiresAt"`
	MinimumUpdaterVersion string              `json:"minimumUpdaterVersion"`
	ReleaseNotesURL       string              `json:"releaseNotesUrl,omitempty"`
	Revision              string              `json:"revision,omitempty"`
	Artifacts             map[string]Artifact `json:"artifacts"`
	Database              DatabasePolicy      `json:"database"`
	Policy                ReleasePolicy       `json:"policy"`
}

func ContainerPlatform() string {
	arch := runtime.GOARCH
	if arch == "x86_64" {
		arch = "amd64"
	}
	return "linux-" + arch
}

func (m Manifest) ArtifactForHost() (Artifact, error) {
	platform := ContainerPlatform()
	artifact, ok := m.Artifacts[platform]
	if !ok {
		return Artifact{}, fmt.Errorf("release does not contain %s", platform)
	}
	return artifact, nil
}

func (m Manifest) Validate(config Config, now time.Time) error {
	if m.SchemaVersion != 1 {
		return fmt.Errorf("unsupported manifest schema %d", m.SchemaVersion)
	}
	if m.Product != ProductName {
		return fmt.Errorf("manifest product %q is not %q", m.Product, ProductName)
	}
	if m.ReleaseID == "" || m.Version == "" || m.Sequence < 1 {
		return fmt.Errorf("releaseId, version, and a positive sequence are required")
	}
	if m.Channel != config.Channel {
		return fmt.Errorf("release channel %q does not match enrolled channel %q", m.Channel, config.Channel)
	}
	if m.ExpiresAt.IsZero() || !now.Before(m.ExpiresAt) {
		return fmt.Errorf("release manifest is expired")
	}
	if m.PublishedAt.After(now.Add(config.ClockSkewAllowance())) {
		return fmt.Errorf("release manifest was published in the future")
	}
	if compareVersions(Version, m.MinimumUpdaterVersion) < 0 {
		return fmt.Errorf("updater %s is older than required %s", Version, m.MinimumUpdaterVersion)
	}
	artifact, err := m.ArtifactForHost()
	if err != nil {
		return err
	}
	if artifact.Repository != config.AllowedImageRepository {
		return fmt.Errorf("image repository %q is not allowed", artifact.Repository)
	}
	if !validSHA256Digest(artifact.Digest) {
		return fmt.Errorf("image digest is not a sha256 digest")
	}
	if m.Database.FromSchemaMinimum > m.Database.FromSchemaMaximum || m.Database.ToSchema < 0 {
		return fmt.Errorf("invalid database compatibility range")
	}
	if m.Policy.DrainTimeoutSeconds < 1 || m.Policy.StartupTimeoutSeconds < 1 || m.Policy.ObservationSeconds < 1 {
		return fmt.Errorf("release timeouts must be positive")
	}
	if m.Policy.ConsecutiveHealthFailureLimit < 1 {
		return fmt.Errorf("health failure limit must be positive")
	}
	return nil
}

func validSHA256Digest(value string) bool {
	if len(value) != len("sha256:")+64 || !strings.HasPrefix(value, "sha256:") {
		return false
	}
	for _, character := range value[len("sha256:"):] {
		if !strings.ContainsRune("0123456789abcdef", character) {
			return false
		}
	}
	return true
}

func compareVersions(left, right string) int {
	parse := func(value string) [3]int {
		var result [3]int
		value = strings.TrimPrefix(value, "v")
		value = strings.SplitN(value, "-", 2)[0]
		_, _ = fmt.Sscanf(value, "%d.%d.%d", &result[0], &result[1], &result[2])
		return result
	}
	a, b := parse(left), parse(right)
	for index := range a {
		if a[index] < b[index] {
			return -1
		}
		if a[index] > b[index] {
			return 1
		}
	}
	return 0
}
