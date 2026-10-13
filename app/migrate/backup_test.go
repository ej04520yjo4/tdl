package migrate

import (
	"bytes"
	"context"
	"encoding/json"
	stderrors "errors"
	"io"
	"os"
	"path/filepath"
	"testing"

	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"

	"github.com/iyear/tdl/pkg/kv"
)

func newBackupTestStorage(t *testing.T) kv.Storage {
	t.Helper()
	store, err := kv.New(kv.DriverFile, map[string]any{
		"path": filepath.Join(t.TempDir(), "state.json"),
	})
	require.NoError(t, err)

	ns, err := store.Open("default")
	require.NoError(t, err)
	require.NoError(t, ns.Set(context.Background(), "api_id", []byte("12345")))
	require.NoError(t, ns.Set(context.Background(), "session", []byte("encrypted-session")))
	t.Cleanup(func() { require.NoError(t, store.Close()) })
	return store
}

func backupTestDir(t *testing.T) string {
	t.Helper()
	// The Windows test sandbox can deny replacement of files below its AppData
	// temp root. Keep the destination in the repository's writable test area so
	// the overwrite path is exercised instead of skipped.
	dir, err := os.MkdirTemp(".", ".backup-test-*")
	require.NoError(t, err)
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
}

func decodeBackupMeta(t *testing.T, path string) kv.Meta {
	t.Helper()
	f, err := os.Open(path)
	require.NoError(t, err)
	defer f.Close()

	dec, err := zstd.NewReader(f)
	require.NoError(t, err)
	defer dec.Close()

	var payload bytes.Buffer
	_, err = dec.WriteTo(&payload)
	require.NoError(t, err)

	var meta kv.Meta
	require.NoError(t, json.Unmarshal(payload.Bytes(), &meta))
	return meta
}

func requireNoBackupTemps(t *testing.T, dst string) {
	t.Helper()
	dir := filepath.Dir(dst)
	base := filepath.Base(dst)
	for _, pattern := range []string{
		filepath.Join(dir, "."+base+"-*.tmp"),
		filepath.Join(dir, "."+base+"-old-*.tmp"),
	} {
		matches, err := filepath.Glob(pattern)
		require.NoError(t, err)
		require.Empty(t, matches, "temporary backup files remain for %s", pattern)
	}
}

func preserveBackupHooks(t *testing.T) {
	t.Helper()
	createTemp := backupCreateTemp
	rename := backupRename
	remove := backupRemove
	syncFile := backupSync
	closeFile := backupClose
	encode := backupEncode
	t.Cleanup(func() {
		backupCreateTemp = createTemp
		backupRename = rename
		backupRemove = remove
		backupSync = syncFile
		backupClose = closeFile
		backupEncode = encode
	})
}

func TestBackupOverwritesExistingDestinationAndRoundTrips(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	old := []byte("old backup that must be replaced")
	require.NoError(t, os.WriteFile(dst, old, 0o666))

	err := Backup(kv.With(context.Background(), store), dst)
	require.NoError(t, err)
	got, err := os.ReadFile(dst)
	require.NoError(t, err)
	require.NotEqual(t, old, got)
	require.Equal(t, kv.Meta{
		"default": {
			"api_id":  []byte("12345"),
			"session": []byte("encrypted-session"),
		},
	}, decodeBackupMeta(t, dst))
	requireNoBackupTemps(t, dst)
}

func TestBackupWriteFailurePreservesDestinationAndCleansTemp(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	old := []byte("keep this backup")
	require.NoError(t, os.WriteFile(dst, old, 0o666))
	writeErr := stderrors.New("simulated encoder write failure")
	backupEncode = func(w io.Writer, _ []byte) error {
		_, err := w.Write([]byte("partial compressed payload"))
		require.NoError(t, err)
		return writeErr
	}

	err := Backup(kv.With(context.Background(), store), dst)
	require.ErrorIs(t, err, writeErr)
	got, readErr := os.ReadFile(dst)
	require.NoError(t, readErr)
	require.Equal(t, old, got)
	requireNoBackupTemps(t, dst)
}

func TestBackupCloseFailurePreservesDestinationAndCleansTemp(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	old := []byte("keep this backup")
	require.NoError(t, os.WriteFile(dst, old, 0o666))
	closeErr := stderrors.New("simulated close failure")
	backupClose = func(f *os.File) error {
		_ = f.Close()
		return closeErr
	}

	err := Backup(kv.With(context.Background(), store), dst)
	require.ErrorIs(t, err, closeErr)
	got, readErr := os.ReadFile(dst)
	require.NoError(t, readErr)
	require.Equal(t, old, got)
	requireNoBackupTemps(t, dst)
}

func TestBackupSyncFailurePreservesDestinationAndCleansTemp(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	old := []byte("keep this backup")
	require.NoError(t, os.WriteFile(dst, old, 0o666))
	syncErr := stderrors.New("simulated sync failure")
	backupSync = func(*os.File) error { return syncErr }

	err := Backup(kv.With(context.Background(), store), dst)
	require.ErrorIs(t, err, syncErr)
	got, readErr := os.ReadFile(dst)
	require.NoError(t, readErr)
	require.Equal(t, old, got)
	requireNoBackupTemps(t, dst)
}

func TestBackupCommitFailurePreservesDestinationAndCleansTemp(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	old := []byte("keep this backup")
	require.NoError(t, os.WriteFile(dst, old, 0o666))

	commitErr := stderrors.New("simulated commit failure")
	renameCalls := 0
	backupRename = func(from, to string) error {
		renameCalls++
		return commitErr
	}

	err := Backup(kv.With(context.Background(), store), dst)
	require.ErrorIs(t, err, commitErr)
	require.Equal(t, 1, renameCalls)
	got, readErr := os.ReadFile(dst)
	require.NoError(t, readErr)
	require.Equal(t, old, got)
	requireNoBackupTemps(t, dst)
}

func TestBackupCommitDirectoryFailurePreservesDestination(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	require.NoError(t, os.Mkdir(dst, 0o700))
	marker := filepath.Join(dst, "keep.txt")
	require.NoError(t, os.WriteFile(marker, []byte("keep"), 0o600))

	err := Backup(kv.With(context.Background(), store), dst)
	require.Error(t, err)
	require.DirExists(t, dst)
	got, readErr := os.ReadFile(marker)
	require.NoError(t, readErr)
	require.Equal(t, []byte("keep"), got)
	requireNoBackupTemps(t, dst)
}

func TestBackupCancellationAfterPreparationPreservesDestination(t *testing.T) {
	preserveBackupHooks(t)
	store := newBackupTestStorage(t)
	dst := filepath.Join(backupTestDir(t), "account.backup.tdl")
	old := []byte("keep this backup")
	require.NoError(t, os.WriteFile(dst, old, 0o666))

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	originalEncode := backupEncode
	backupEncode = func(w io.Writer, meta []byte) error {
		err := originalEncode(w, meta)
		cancel()
		return err
	}

	err := Backup(kv.With(ctx, store), dst)
	require.ErrorIs(t, err, context.Canceled)
	got, readErr := os.ReadFile(dst)
	require.NoError(t, readErr)
	require.Equal(t, old, got)
	requireNoBackupTemps(t, dst)
}
