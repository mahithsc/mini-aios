package update

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/BurntSushi/toml"
)

const canonicalDatabaseRelativePath = "state/aios.db"

type Config struct {
	Channel                 string `toml:"channel"`
	FeedURL                 string `toml:"feed_url"`
	PublicKeyPath           string `toml:"public_key_path"`
	AllowedImageRepository  string `toml:"allowed_image_repository"`
	ComposeProjectDir       string `toml:"compose_project_dir"`
	ComposeService          string `toml:"compose_service"`
	ReleaseEnvPath          string `toml:"release_env_path"`
	AIOSDataDir             string `toml:"aios_data_dir"`
	DatabaseRelativePath    string `toml:"database_relative_path"`
	StateDir                string `toml:"state_dir"`
	HealthURL               string `toml:"health_url"`
	UpdaterTokenFile        string `toml:"updater_token_file"`
	DockerBinary            string `toml:"docker_binary"`
	PollIntervalValue       string `toml:"poll_interval"`
	PollJitterValue         string `toml:"poll_jitter"`
	MinimumFreeBytes        uint64 `toml:"minimum_free_bytes"`
	BackupRetention         int    `toml:"backup_retention"`
	MaximumDrainValue       string `toml:"maximum_drain_timeout"`
	MaximumStartupValue     string `toml:"maximum_startup_timeout"`
	MaximumObservationValue string `toml:"maximum_observation_period"`
	ClockSkewValue          string `toml:"clock_skew_allowance"`
	AllowDevelopmentHost    bool   `toml:"allow_development_host"`
}

func LoadConfig(path string) (Config, error) {
	var config Config
	if _, err := toml.DecodeFile(path, &config); err != nil {
		return Config{}, fmt.Errorf("decode config: %w", err)
	}
	config.applyDefaults()
	if err := config.Validate(); err != nil {
		return Config{}, err
	}
	return config, nil
}

func (c *Config) applyDefaults() {
	if c.Channel == "" {
		c.Channel = "stable"
	}
	if c.ComposeService == "" {
		c.ComposeService = "box"
	}
	if c.ReleaseEnvPath == "" && c.ComposeProjectDir != "" {
		c.ReleaseEnvPath = filepath.Join(c.ComposeProjectDir, "release.env")
	}
	if c.DatabaseRelativePath == "" {
		c.DatabaseRelativePath = filepath.FromSlash(canonicalDatabaseRelativePath)
	}
	if c.DockerBinary == "" {
		c.DockerBinary = "docker"
	}
	if c.PollIntervalValue == "" {
		c.PollIntervalValue = "30m"
	}
	if c.PollJitterValue == "" {
		c.PollJitterValue = "30m"
	}
	if c.BackupRetention == 0 {
		c.BackupRetention = 2
	}
	if c.MaximumDrainValue == "" {
		c.MaximumDrainValue = "10m"
	}
	if c.MaximumStartupValue == "" {
		c.MaximumStartupValue = "5m"
	}
	if c.MaximumObservationValue == "" {
		c.MaximumObservationValue = "30m"
	}
	if c.ClockSkewValue == "" {
		c.ClockSkewValue = "5m"
	}
}

func (c Config) Validate() error {
	required := map[string]string{
		"feed_url":                 c.FeedURL,
		"public_key_path":          c.PublicKeyPath,
		"allowed_image_repository": c.AllowedImageRepository,
		"compose_project_dir":      c.ComposeProjectDir,
		"release_env_path":         c.ReleaseEnvPath,
		"aios_data_dir":            c.AIOSDataDir,
		"state_dir":                c.StateDir,
		"health_url":               c.HealthURL,
		"updater_token_file":       c.UpdaterTokenFile,
	}
	for name, value := range required {
		if value == "" {
			return fmt.Errorf("config %s is required", name)
		}
	}
	for name, value := range map[string]string{
		"poll_interval":              c.PollIntervalValue,
		"poll_jitter":                c.PollJitterValue,
		"maximum_drain_timeout":      c.MaximumDrainValue,
		"maximum_startup_timeout":    c.MaximumStartupValue,
		"maximum_observation_period": c.MaximumObservationValue,
		"clock_skew_allowance":       c.ClockSkewValue,
	} {
		if _, err := time.ParseDuration(value); err != nil {
			return fmt.Errorf("config %s: %w", name, err)
		}
	}
	if runtime.GOOS != "linux" && !c.AllowDevelopmentHost {
		return fmt.Errorf("host %s is development-only; set allow_development_host=true", runtime.GOOS)
	}
	for _, path := range []string{c.PublicKeyPath, c.ComposeProjectDir, c.ReleaseEnvPath, c.AIOSDataDir, c.StateDir, c.UpdaterTokenFile} {
		if !filepath.IsAbs(path) {
			return fmt.Errorf("security-sensitive paths must be absolute: %s", path)
		}
	}
	if _, err := cleanDatabaseRelativePath(c.DatabaseRelativePath); err != nil {
		return fmt.Errorf("config database_relative_path: %w", err)
	}
	return nil
}

func parseDuration(value string) time.Duration {
	duration, _ := time.ParseDuration(value)
	return duration
}

func (c Config) PollInterval() time.Duration       { return parseDuration(c.PollIntervalValue) }
func (c Config) PollJitter() time.Duration         { return parseDuration(c.PollJitterValue) }
func (c Config) MaximumDrain() time.Duration       { return parseDuration(c.MaximumDrainValue) }
func (c Config) MaximumStartup() time.Duration     { return parseDuration(c.MaximumStartupValue) }
func (c Config) MaximumObservation() time.Duration { return parseDuration(c.MaximumObservationValue) }
func (c Config) ClockSkewAllowance() time.Duration { return parseDuration(c.ClockSkewValue) }

func (c Config) EnsureDirectories() error {
	for _, path := range []string{c.StateDir, filepath.Join(c.StateDir, "backups"), c.AIOSDataDir} {
		if err := os.MkdirAll(path, 0o700); err != nil {
			return err
		}
	}
	return nil
}
