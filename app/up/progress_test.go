package up

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/go-faster/errors"
	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
	prettyprogress "github.com/jedib0t/go-pretty/v6/progress"
	"github.com/stretchr/testify/require"

	"github.com/iyear/tdl/pkg/desktopio"
)

type machineEvent struct {
	Event    string  `json:"event"`
	FileID   string  `json:"file_id"`
	State    string  `json:"state"`
	Percent  float64 `json:"percent"`
	Filename string  `json:"filename"`
	Message  string  `json:"message"`
}

func machineEvents(t *testing.T, output string) []machineEvent {
	t.Helper()
	var events []machineEvent
	for _, line := range strings.Split(output, "\n") {
		if !strings.HasPrefix(line, desktopio.Prefix) {
			continue
		}
		var got machineEvent
		require.NoError(t, json.Unmarshal([]byte(strings.TrimPrefix(line, desktopio.Prefix)), &got))
		events = append(events, got)
	}
	return events
}

func newTestElem(t *testing.T, dir, name string, remove bool) *iterElem {
	t.Helper()
	path := filepath.Join(dir, name)
	require.NoError(t, os.WriteFile(path, []byte("data"), 0o600))
	file, err := os.Open(path)
	require.NoError(t, err)
	t.Cleanup(func() { _ = file.Close() })
	return &iterElem{
		file:   &uploaderFile{File: file, size: 4},
		to:     (&peers.Options{}).Build(nil).Chat(&tg.Chat{ID: 9, Title: "test"}),
		remove: remove,
	}
}

// --rm must only delete a local file after Telegram accepted it.
func TestProgressKeepsLocalFileWhenUploadFails(t *testing.T) {
	dir := t.TempDir()
	elem := newTestElem(t, dir, "failed.bin", true)

	var output bytes.Buffer
	ctx, bridge := desktopio.New(context.Background(), nil, &output)
	defer bridge.Close()
	p := newProgress(ctx, prettyprogress.NewWriter())

	p.OnAdd(elem)
	p.OnDone(elem, errors.New("simulated send failure"))

	require.Equal(t, int64(1), p.Failures())
	require.FileExists(t, filepath.Join(dir, "failed.bin"))

	events := machineEvents(t, output.String())
	require.Len(t, events, 2)
	require.Equal(t, "queued", events[0].State)
	require.Equal(t, "error", events[1].Event)
	require.Equal(t, "failed", events[1].State)
	require.Equal(t, events[0].FileID, events[1].FileID)
	require.Contains(t, events[1].Message, "failed.bin")
}

func TestProgressRemovesLocalFileAndReportsCompletionAfterUpload(t *testing.T) {
	dir := t.TempDir()
	elem := newTestElem(t, dir, "done.bin", true)

	var output bytes.Buffer
	ctx, bridge := desktopio.New(context.Background(), nil, &output)
	defer bridge.Close()
	p := newProgress(ctx, prettyprogress.NewWriter())

	p.OnAdd(elem)
	p.OnDone(elem, nil)

	require.Equal(t, int64(0), p.Failures())
	require.NoFileExists(t, filepath.Join(dir, "done.bin"))

	events := machineEvents(t, output.String())
	require.Len(t, events, 2)
	require.Equal(t, "completed", events[1].State)
	require.Equal(t, 100.0, events[1].Percent)
	require.Equal(t, "done.bin", events[1].Filename)
}

func TestProgressDoesNotCountCancellationAsFailure(t *testing.T) {
	dir := t.TempDir()
	elem := newTestElem(t, dir, "cancelled.bin", true)

	var output bytes.Buffer
	ctx, bridge := desktopio.New(context.Background(), nil, &output)
	defer bridge.Close()
	p := newProgress(ctx, prettyprogress.NewWriter())

	p.OnAdd(elem)
	p.OnDone(elem, errors.Wrap(context.Canceled, "upload"))

	require.Equal(t, int64(0), p.Failures())
	require.FileExists(t, filepath.Join(dir, "cancelled.bin"))
}
