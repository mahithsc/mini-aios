package update

import (
	"context"
	"fmt"
	"log"
	"path/filepath"
	"time"
)

type Engine struct {
	Config Config
	Feed   FeedClient
	System System
	Store  StateStore
	Logger *log.Logger
}

func NewEngine(config Config, logger *log.Logger) (*Engine, error) {
	if err := config.EnsureDirectories(); err != nil {
		return nil, err
	}
	return &Engine{
		Config: config,
		Feed:   FeedClient{},
		System: System{Config: config},
		Store:  StateStore{Path: filepath.Join(config.StateDir, "state.json")},
		Logger: logger,
	}, nil
}

func (e *Engine) Status() (State, error) {
	state, err := e.Store.Load()
	if err != nil {
		return State{}, err
	}
	if state.Current == nil {
		state.Current = e.System.ReadSelectedRelease()
	}
	return state, nil
}

func (e *Engine) Check(ctx context.Context) (Manifest, bool, error) {
	manifest, err := e.Feed.Fetch(ctx, e.Config)
	if err != nil {
		return Manifest{}, false, err
	}
	state, err := e.Status()
	if err != nil {
		return Manifest{}, false, err
	}
	if state.Current != nil && manifest.Sequence <= state.Current.Sequence {
		return manifest, false, nil
	}
	if manifest.Sequence == state.LastFailedSequence {
		return manifest, false, nil
	}
	if state.Status == "recovery_required" {
		return Manifest{}, false, fmt.Errorf("updater is in recovery_required")
	}
	if state.Current != nil {
		schema := state.Current.DatabaseSchema
		if schema < manifest.Database.FromSchemaMinimum || schema > manifest.Database.FromSchemaMaximum {
			return Manifest{}, false, fmt.Errorf("database schema %d is outside release compatibility %d..%d", schema, manifest.Database.FromSchemaMinimum, manifest.Database.FromSchemaMaximum)
		}
	}
	return manifest, true, nil
}

// BootstrapLatest installs the first signed release on a device where no AIOS
// release is running yet. Normal updates must use InstallLatest so they retain
// the drain, backup, activation, observation, and rollback transaction.
func (e *Engine) BootstrapLatest(ctx context.Context, expectedReleaseID string) error {
	lock, err := AcquireLock(filepath.Join(e.Config.StateDir, "update.lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := e.recoverLocked(ctx); err != nil {
		return err
	}

	state, err := e.Status()
	if err != nil {
		return err
	}
	if state.Current != nil {
		if expectedReleaseID == "" || state.Current.ReleaseID == expectedReleaseID {
			return nil
		}
		return fmt.Errorf("device is already bootstrapped with release %s", state.Current.ReleaseID)
	}

	manifest, available, err := e.Check(ctx)
	if err != nil {
		return err
	}
	if !available {
		return fmt.Errorf("signed feed does not contain an eligible bootstrap release")
	}
	if expectedReleaseID != "" && manifest.ReleaseID != expectedReleaseID {
		return fmt.Errorf("latest signed release is %s, not %s", manifest.ReleaseID, expectedReleaseID)
	}
	artifact, err := manifest.ArtifactForHost()
	if err != nil {
		return err
	}
	target := ReleaseFromManifest(manifest, artifact)
	state.Attempt++
	state.Target = &target
	state.Bootstrap = true
	state.LastError = ""
	state.LastFailedSequence = 0
	if err := e.transition(&state, "downloading"); err != nil {
		return err
	}
	if err := e.System.PullAndVerify(ctx, artifact); err != nil {
		return e.failBeforeActivation(&state, err)
	}

	if err := e.transition(&state, "preflight"); err != nil {
		return err
	}
	freeBytes, err := e.System.FreeBytes()
	if err != nil {
		return e.failBeforeActivation(&state, fmt.Errorf("check free disk: %w", err))
	}
	requiredBytes := e.Config.MinimumFreeBytes + uint64(max64(artifact.SizeBytes, 0))
	if freeBytes < requiredBytes {
		return e.failBeforeActivation(&state, fmt.Errorf("insufficient disk: have %d, require %d", freeBytes, requiredBytes))
	}

	if err := e.transition(&state, "activating"); err != nil {
		return err
	}
	if err := e.System.WriteReleaseEnv(target); err != nil {
		return e.failBootstrap(ctx, &state, err)
	}
	if err := e.System.Start(ctx); err != nil {
		return e.failBootstrap(ctx, &state, err)
	}
	startupTimeout := boundedDuration(time.Duration(manifest.Policy.StartupTimeoutSeconds)*time.Second, e.Config.MaximumStartup())
	if err := e.System.WaitReady(ctx, target, startupTimeout); err != nil {
		return e.failBootstrap(ctx, &state, err)
	}

	if err := e.transition(&state, "observing"); err != nil {
		return err
	}
	observation := boundedDuration(time.Duration(manifest.Policy.ObservationSeconds)*time.Second, e.Config.MaximumObservation())
	if err := e.System.Observe(ctx, target, observation, manifest.Policy.ConsecutiveHealthFailureLimit); err != nil {
		return e.failBootstrap(ctx, &state, err)
	}

	state.Current = &target
	state.Target = nil
	state.Bootstrap = false
	state.LastError = ""
	if err := e.transition(&state, "committed"); err != nil {
		return err
	}
	return e.transition(&state, "idle")
}

func (e *Engine) InstallLatest(ctx context.Context, expectedReleaseID string) error {
	lock, err := AcquireLock(filepath.Join(e.Config.StateDir, "update.lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	if err := e.recoverLocked(ctx); err != nil {
		return err
	}

	manifest, available, err := e.Check(ctx)
	if err != nil {
		return err
	}
	if !available {
		return nil
	}
	if expectedReleaseID != "" && manifest.ReleaseID != expectedReleaseID {
		return fmt.Errorf("latest signed release is %s, not %s", manifest.ReleaseID, expectedReleaseID)
	}
	artifact, _ := manifest.ArtifactForHost()
	target := ReleaseFromManifest(manifest, artifact)
	state, err := e.Status()
	if err != nil {
		return err
	}
	state.Attempt++
	state.Target = &target
	state.RestoreBackupOnRollback = manifest.Database.RestoreBackupOnRollback
	state.LastError = ""
	state.LastFailedSequence = 0
	if err := e.transition(&state, "downloading"); err != nil {
		return err
	}
	if err := e.System.PullAndVerify(ctx, artifact); err != nil {
		return e.failBeforeActivation(&state, err)
	}

	if err := e.transition(&state, "preflight"); err != nil {
		return err
	}
	freeBytes, err := e.System.FreeBytes()
	if err != nil {
		return e.failBeforeActivation(&state, fmt.Errorf("check free disk: %w", err))
	}
	requiredBytes := e.Config.MinimumFreeBytes + uint64(max64(artifact.SizeBytes, 0))
	if freeBytes < requiredBytes {
		return e.failBeforeActivation(&state, fmt.Errorf("insufficient disk: have %d, require %d", freeBytes, requiredBytes))
	}

	if err := e.transition(&state, "draining"); err != nil {
		return err
	}
	drainTimeout := boundedDuration(time.Duration(manifest.Policy.DrainTimeoutSeconds)*time.Second, e.Config.MaximumDrain())
	if err := e.System.Drain(ctx, drainTimeout); err != nil {
		_ = e.System.Resume(ctx)
		return e.failBeforeActivation(&state, err)
	}

	if err := e.System.Stop(ctx); err != nil {
		_ = e.System.Resume(ctx)
		return e.failBeforeActivation(&state, err)
	}
	if err := e.transition(&state, "backing_up"); err != nil {
		_ = e.System.Start(ctx)
		_ = e.System.Resume(ctx)
		return err
	}
	backupDir, err := e.System.Backup(manifest.ReleaseID)
	if err != nil {
		_ = e.System.Start(ctx)
		_ = e.System.Resume(ctx)
		return e.failBeforeActivation(&state, err)
	}
	state.BackupDir = backupDir
	if err := e.Store.Save(state); err != nil {
		_ = e.System.Start(ctx)
		_ = e.System.Resume(ctx)
		return err
	}

	if err := e.transition(&state, "activating"); err != nil {
		return err
	}
	if err := e.System.WriteReleaseEnv(target); err != nil {
		return e.rollback(ctx, &state, err)
	}
	if err := e.System.Start(ctx); err != nil {
		return e.rollback(ctx, &state, err)
	}
	startupTimeout := boundedDuration(time.Duration(manifest.Policy.StartupTimeoutSeconds)*time.Second, e.Config.MaximumStartup())
	if err := e.System.WaitReady(ctx, target, startupTimeout); err != nil {
		return e.rollback(ctx, &state, err)
	}

	if err := e.transition(&state, "observing"); err != nil {
		return err
	}
	observation := boundedDuration(time.Duration(manifest.Policy.ObservationSeconds)*time.Second, e.Config.MaximumObservation())
	if err := e.System.Observe(ctx, target, observation, manifest.Policy.ConsecutiveHealthFailureLimit); err != nil {
		return e.rollback(ctx, &state, err)
	}

	state.Previous = state.Current
	state.Current = &target
	state.Target = nil
	state.RestoreBackupOnRollback = false
	state.LastError = ""
	if err := e.transition(&state, "committed"); err != nil {
		return err
	}
	if err := e.System.Resume(ctx); err != nil {
		state.LastError = "release committed but resume failed: " + err.Error()
		_ = e.Store.Save(state)
		return fmt.Errorf("%s", state.LastError)
	}
	return e.transition(&state, "idle")
}

func (e *Engine) rollback(ctx context.Context, state *State, cause error) error {
	state.LastError = cause.Error()
	if state.Target != nil {
		state.LastFailedSequence = state.Target.Sequence
	}
	if err := e.transition(state, "rolling_back"); err != nil {
		return err
	}
	_ = e.System.Stop(ctx)
	if state.RestoreBackupOnRollback {
		if err := e.System.Restore(state.BackupDir); err != nil {
			state.LastError = fmt.Sprintf("%v; database restore failed: %v", cause, err)
			_ = e.transition(state, "recovery_required")
			return fmt.Errorf("update failed and database restore failed: %w", err)
		}
	}
	if state.Current == nil || state.Current.Image == "" {
		state.LastError = fmt.Sprintf("%v; previous release is unknown", cause)
		_ = e.transition(state, "recovery_required")
		return fmt.Errorf("update failed and previous release is unknown: %w", cause)
	}
	if err := e.System.WriteReleaseEnv(*state.Current); err != nil {
		state.LastError = fmt.Sprintf("%v; select previous release failed: %v", cause, err)
		_ = e.transition(state, "recovery_required")
		return cause
	}
	if err := e.System.Start(ctx); err != nil {
		state.LastError = fmt.Sprintf("%v; previous release start failed: %v", cause, err)
		_ = e.transition(state, "recovery_required")
		return cause
	}
	if err := e.System.WaitReady(ctx, *state.Current, e.Config.MaximumStartup()); err != nil {
		state.LastError = fmt.Sprintf("%v; previous release unhealthy: %v", cause, err)
		_ = e.transition(state, "recovery_required")
		return cause
	}
	state.Target = nil
	state.RestoreBackupOnRollback = false
	if err := e.System.Resume(ctx); err != nil {
		state.LastError = fmt.Sprintf("%v; previous release is healthy but resume failed: %v", cause, err)
		_ = e.Store.Save(*state)
		return fmt.Errorf("%s", state.LastError)
	}
	if err := e.transition(state, "rolled_back"); err != nil {
		return err
	}
	return fmt.Errorf("release rolled back: %w", cause)
}

func (e *Engine) failBeforeActivation(state *State, cause error) error {
	state.LastError = cause.Error()
	state.Target = nil
	state.RestoreBackupOnRollback = false
	state.Bootstrap = false
	_ = e.transition(state, "failed")
	return cause
}

func (e *Engine) failBootstrap(ctx context.Context, state *State, cause error) error {
	_ = e.System.Stop(ctx)
	removeError := e.System.ClearReleaseEnv()
	state.LastError = cause.Error()
	if removeError != nil {
		state.LastError += "; clear selected release: " + removeError.Error()
	}
	state.Target = nil
	state.RestoreBackupOnRollback = false
	state.Bootstrap = false
	_ = e.transition(state, "failed")
	return fmt.Errorf("bootstrap failed: %w", cause)
}

func (e *Engine) recoverLocked(ctx context.Context) error {
	state, err := e.Store.Load()
	if err != nil {
		return err
	}
	if state.Bootstrap {
		return e.recoverBootstrap(ctx, &state)
	}
	switch state.Status {
	case "", "idle", "failed", "rolled_back":
		return nil
	case "committed":
		if err := e.System.Resume(ctx); err != nil {
			return fmt.Errorf("resume committed release: %w", err)
		}
		return e.transition(&state, "idle")
	case "downloading", "preflight", "awaiting_window":
		state.Target = nil
		state.RestoreBackupOnRollback = false
		return e.transition(&state, "idle")
	case "draining":
		if err := e.System.Resume(ctx); err != nil {
			return fmt.Errorf("resume interrupted drain: %w", err)
		}
		state.Target = nil
		state.RestoreBackupOnRollback = false
		return e.transition(&state, "idle")
	case "backing_up":
		if state.Current == nil {
			state.LastError = "cannot recover backup phase without previous release"
			_ = e.transition(&state, "recovery_required")
			return fmt.Errorf("%s", state.LastError)
		}
		recoveryErr := e.System.WriteReleaseEnv(*state.Current)
		if recoveryErr == nil {
			recoveryErr = e.System.Start(ctx)
		}
		if recoveryErr == nil {
			recoveryErr = e.System.WaitReady(ctx, *state.Current, e.Config.MaximumStartup())
		}
		if recoveryErr != nil {
			state.LastError = "failed to restore previous release during recovery: " + recoveryErr.Error()
			_ = e.transition(&state, "recovery_required")
			return recoveryErr
		}
		if err := e.System.Resume(ctx); err != nil {
			return fmt.Errorf("resume restored release: %w", err)
		}
		state.Target = nil
		state.RestoreBackupOnRollback = false
		return e.transition(&state, "idle")
	case "activating", "observing":
		if state.Target == nil {
			state.LastError = "activation recovery has no target release"
			_ = e.transition(&state, "recovery_required")
			return fmt.Errorf("%s", state.LastError)
		}
		if err := e.System.WaitReady(ctx, *state.Target, e.Config.MaximumStartup()); err != nil {
			return e.rollback(ctx, &state, fmt.Errorf("activation recovery health check failed: %w", err))
		}
		if err := e.System.Observe(ctx, *state.Target, e.Config.MaximumObservation(), 3); err != nil {
			return e.rollback(ctx, &state, fmt.Errorf("activation recovery observation failed: %w", err))
		}
		state.Previous = state.Current
		state.Current = state.Target
		state.Target = nil
		state.RestoreBackupOnRollback = false
		if err := e.transition(&state, "committed"); err != nil {
			return err
		}
		if err := e.System.Resume(ctx); err != nil {
			state.LastError = "release committed during recovery but resume failed: " + err.Error()
			_ = e.Store.Save(state)
			return fmt.Errorf("%s", state.LastError)
		}
		return e.transition(&state, "idle")
	case "rolling_back":
		return e.rollback(ctx, &state, fmt.Errorf("resuming interrupted rollback"))
	case "recovery_required":
		return fmt.Errorf("updater requires local recovery: %s", state.LastError)
	default:
		return fmt.Errorf("unknown persisted updater state %q", state.Status)
	}
}

func (e *Engine) recoverBootstrap(ctx context.Context, state *State) error {
	switch state.Status {
	case "downloading", "preflight":
		state.Target = nil
		state.Bootstrap = false
		return e.transition(state, "idle")
	case "activating", "observing":
		if state.Target == nil {
			state.Bootstrap = false
			state.LastError = "bootstrap recovery has no target release"
			_ = e.transition(state, "failed")
			return fmt.Errorf("%s", state.LastError)
		}
		if err := e.System.WriteReleaseEnv(*state.Target); err != nil {
			return e.failBootstrap(ctx, state, fmt.Errorf("recover bootstrap selection: %w", err))
		}
		if err := e.System.Start(ctx); err != nil {
			return e.failBootstrap(ctx, state, fmt.Errorf("recover bootstrap start: %w", err))
		}
		if err := e.System.WaitReady(ctx, *state.Target, e.Config.MaximumStartup()); err != nil {
			return e.failBootstrap(ctx, state, fmt.Errorf("recover bootstrap health check: %w", err))
		}
		if err := e.System.Observe(ctx, *state.Target, e.Config.MaximumObservation(), 3); err != nil {
			return e.failBootstrap(ctx, state, fmt.Errorf("recover bootstrap observation: %w", err))
		}
		state.Current = state.Target
		state.Target = nil
		state.Bootstrap = false
		state.LastError = ""
		if err := e.transition(state, "committed"); err != nil {
			return err
		}
		return e.transition(state, "idle")
	default:
		state.Bootstrap = false
		state.LastError = fmt.Sprintf("cannot recover bootstrap from state %q", state.Status)
		_ = e.transition(state, "failed")
		return fmt.Errorf("%s", state.LastError)
	}
}

func (e *Engine) transition(state *State, status string) error {
	state.Status = status
	if e.Logger != nil {
		e.Logger.Printf("update state=%s release=%v", status, state.Target)
	}
	return e.Store.Save(*state)
}

func boundedDuration(requested, maximum time.Duration) time.Duration {
	if requested <= 0 || requested > maximum {
		return maximum
	}
	return requested
}

func max64(left int64, right int64) int64 {
	if left > right {
		return left
	}
	return right
}
