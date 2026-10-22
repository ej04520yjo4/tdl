package dl

import (
	"bytes"
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	prettyprogress "github.com/jedib0t/go-pretty/v6/progress"
	"github.com/stretchr/testify/require"

	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
	"github.com/iyear/tdl/core/downloader"
	"github.com/iyear/tdl/core/tmedia"
	"github.com/iyear/tdl/pkg/desktopio"
)

func TestProgressPostProcessingFailureDoesNotCheckpointOrReportComplete(t *testing.T) {
	root := t.TempDir()
	tempPath := filepath.Join(root, "broken.bin"+tempExt)
	outputPath := filepath.Join(root, "broken.bin")

	file, err := os.Create(tempPath)
	require.NoError(t, err)
	_, err = file.Write([]byte("x"))
	require.NoError(t, err)
	// A directory at the final path makes os.Rename fail on supported desktop
	// platforms after the temporary file has been closed.
	require.NoError(t, os.Mkdir(outputPath, 0o755))

	elem := &iterElem{
		id:         1,
		logicalPos: 7,
		file:       &tmedia.Media{Name: "broken.bin", Size: 1},
		to:         file,
	}
	it := &iter{mu: &sync.Mutex{}, finished: make(map[int]struct{})}

	var output bytes.Buffer
	ctx, bridge := desktopio.New(context.Background(), nil, &output)
	defer bridge.Close()
	p := newProgress(ctx, prettyprogress.NewWriter(), it, Options{})
	tracker := &prettyprogress.Tracker{
		Message: "broken.bin",
		Total:   1,
		Units:   prettyprogress.UnitsBytes,
	}
	p.pw.AppendTracker(tracker)
	p.trackers.Store(elem.id, tracker)
	p.OnDone(elem, nil)

	require.Equal(t, int64(1), p.Failures())
	require.DirExists(t, outputPath)
	require.FileExists(t, tempPath)
	if _, finished := it.finished[elem.logicalPos]; finished {
		t.Fatalf("logical position %d was checkpointed after post-processing failure", elem.logicalPos)
	}

	var errorMessage string
	for _, line := range strings.Split(output.String(), "\n") {
		if !strings.HasPrefix(line, desktopio.Prefix) {
			continue
		}
		var event map[string]any
		require.NoError(t, json.Unmarshal([]byte(strings.TrimPrefix(line, desktopio.Prefix)), &event))
		switch event["event"] {
		case "progress":
			if percent, ok := event["percent"].(float64); ok && percent >= 100 {
				t.Fatalf("post-processing failure emitted completion progress: %v", percent)
			}
		case "error":
			errorMessage, _ = event["message"].(string)
		}
	}
	require.Contains(t, errorMessage, "broken.bin")
}

func TestProgressMachineEventsIdentifyFileAndCompleteAfterPostProcessing(t *testing.T) {
	// The Windows test sandbox denies renames below its TempDir path. Keep
	// this fixture under the package workspace so OnDone can exercise the real
	// rename/checkpoint path.
	root, err := os.MkdirTemp(".", "progress-machine-")
	require.NoError(t, err)
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	tempPath := filepath.Join(root, "done.bin"+tempExt)
	file, err := os.Create(tempPath)
	require.NoError(t, err)
	_, err = file.Write([]byte("x"))
	require.NoError(t, err)

	elem := &iterElem{
		id:         4,
		logicalPos: 12,
		from:       (&peers.Options{}).Build(nil).Chat(&tg.Chat{ID: 9, Title: "test"}),
		fromMsg:    &tg.Message{ID: 42},
		file:       &tmedia.Media{Name: "done.bin", Size: 1},
		to:         file,
	}
	it := &iter{mu: &sync.Mutex{}, finished: make(map[int]struct{})}

	var output bytes.Buffer
	ctx, bridge := desktopio.New(context.Background(), nil, &output)
	defer bridge.Close()
	p := newProgress(ctx, prettyprogress.NewWriter(), it, Options{})
	p.OnAdd(elem)
	p.OnDownload(elem, downloader.ProgressState{Downloaded: 1, Total: 1})
	p.OnDone(elem, nil)

	type event struct {
		Event   string  `json:"event"`
		FileID  string  `json:"file_id"`
		State   string  `json:"state"`
		Percent float64 `json:"percent"`
		Path    string  `json:"path"`
	}
	var events []event
	for _, line := range strings.Split(output.String(), "\n") {
		if !strings.HasPrefix(line, desktopio.Prefix) {
			continue
		}
		var got event
		require.NoError(t, json.Unmarshal([]byte(strings.TrimPrefix(line, desktopio.Prefix)), &got))
		if got.Event == "progress" {
			events = append(events, got)
		}
	}
	require.Len(t, events, 3)
	require.Equal(t, "12", events[0].FileID)
	require.Equal(t, "queued", events[0].State)
	require.Equal(t, "downloading", events[1].State)
	require.Equal(t, "completed", events[2].State)
	require.Equal(t, 100.0, events[2].Percent)
	// Every state names the folder the file lives in, so the desktop can open
	// the per-chat folder instead of the download root.
	wantFolder, err := filepath.Abs(root)
	require.NoError(t, err)
	for _, got := range events {
		require.Equal(t, wantFolder, got.Path)
	}
	require.FileExists(t, filepath.Join(root, "done.bin"))
	_, finished := it.finished[elem.logicalPos]
	require.True(t, finished, "logical position should be checkpointed after completion")
}
