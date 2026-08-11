package main

import (
	"context"
	cryptorand "crypto/rand"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/mahithsc/mini-aios/updater/internal/update"
)

func usage() {
	fmt.Fprintf(os.Stderr, "usage: %s <daemon|status|check|bootstrap|install|doctor|version> [options]\n", filepath.Base(os.Args[0]))
}

func commandConfig(arguments []string) (string, string, error) {
	set := flag.NewFlagSet("mini-aios-updater", flag.ContinueOnError)
	defaultConfig := "/etc/mini-aios/updater.toml"
	if home, err := os.UserHomeDir(); err == nil && filepath.Base(os.Args[0]) != "mini-aios-updater-linux" {
		if candidate := filepath.Join(home, ".config", "mini-aios", "updater.toml"); fileExists(candidate) {
			defaultConfig = candidate
		}
	}
	configPath := set.String("config", defaultConfig, "path to updater TOML configuration")
	releaseID := set.String("release-id", "", "require this signed release ID")
	if err := set.Parse(arguments); err != nil {
		return "", "", err
	}
	return *configPath, *releaseID, nil
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	command := os.Args[1]
	if command == "version" {
		fmt.Printf("mini-aios-updater %s (%s) %s/%s\n", update.Version, update.Revision, runtimeGOOS(), runtimeGOARCH())
		return
	}
	configPath, releaseID, err := commandConfig(os.Args[2:])
	if err != nil {
		log.Fatal(err)
	}
	config, err := update.LoadConfig(configPath)
	if err != nil {
		log.Fatal(err)
	}
	logger := log.New(os.Stdout, "mini-aios-updater: ", log.LstdFlags|log.LUTC)
	engine, err := update.NewEngine(config, logger)
	if err != nil {
		log.Fatal(err)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	switch command {
	case "status":
		state, err := engine.Status()
		if err != nil {
			log.Fatal(err)
		}
		data, _ := json.MarshalIndent(state, "", "  ")
		fmt.Println(string(data))
	case "check":
		manifest, available, err := engine.Check(ctx)
		if err != nil {
			log.Fatal(err)
		}
		fmt.Printf("release=%s version=%s sequence=%d available=%t platform=%s\n", manifest.ReleaseID, manifest.Version, manifest.Sequence, available, update.ContainerPlatform())
	case "bootstrap":
		if err := engine.BootstrapLatest(ctx, releaseID); err != nil {
			log.Fatal(err)
		}
	case "install":
		if err := engine.InstallLatest(ctx, releaseID); err != nil {
			log.Fatal(err)
		}
	case "doctor":
		state, stateErr := engine.Status()
		manifest, available, feedErr := engine.Check(ctx)
		result := map[string]any{
			"platform":      update.ContainerPlatform(),
			"updater":       update.Version,
			"state":         state,
			"stateError":    errorString(stateErr),
			"feedRelease":   manifest.ReleaseID,
			"feedAvailable": available,
			"feedError":     errorString(feedErr),
		}
		data, _ := json.MarshalIndent(result, "", "  ")
		fmt.Println(string(data))
		if stateErr != nil || feedErr != nil {
			os.Exit(1)
		}
	case "daemon":
		runDaemon(ctx, engine, config, logger)
	default:
		usage()
		os.Exit(2)
	}
}

func runDaemon(ctx context.Context, engine *update.Engine, config update.Config, logger *log.Logger) {
	seedBytes := make([]byte, 8)
	_, _ = cryptorand.Read(seedBytes)
	random := rand.New(rand.NewSource(int64(binary.LittleEndian.Uint64(seedBytes))))
	for {
		if err := engine.InstallLatest(ctx, ""); err != nil && ctx.Err() == nil {
			logger.Printf("update check/install failed: %v", err)
		}
		delay := config.PollInterval()
		if jitter := config.PollJitter(); jitter > 0 {
			delay += time.Duration(random.Int63n(int64(jitter)))
		}
		timer := time.NewTimer(delay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

func errorString(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}
