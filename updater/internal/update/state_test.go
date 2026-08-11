package update

import (
	"path/filepath"
	"testing"
)

func TestStateStoreRoundTrip(t *testing.T) {
	store := StateStore{Path: filepath.Join(t.TempDir(), "state.json")}
	state := InitialState()
	state.Status = "observing"
	state.Current = &ReleaseRef{ReleaseID: "old", Sequence: 1}
	state.Target = &ReleaseRef{ReleaseID: "new", Sequence: 2}
	if err := store.Save(state); err != nil {
		t.Fatal(err)
	}
	loaded, err := store.Load()
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Status != "observing" || loaded.Target == nil || loaded.Target.ReleaseID != "new" {
		t.Fatalf("unexpected state: %#v", loaded)
	}
}
