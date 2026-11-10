package dl

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	prettyprogress "github.com/jedib0t/go-pretty/v6/progress"
	"github.com/stretchr/testify/require"

	"github.com/gotd/td/tg"
	"github.com/iyear/tdl/core/downloader"
	"github.com/iyear/tdl/core/tmedia"
	"github.com/iyear/tdl/pkg/desktopio"
)

const partTestSize = 2*downloader.MaxPartSize + 1000

func partMedia(id int64) *tmedia.Media {
	return &tmedia.Media{
		Name:         "video.mp4",
		Size:         partTestSize,
		InputFileLoc: &tg.InputDocumentFileLocation{ID: id},
	}
}

// pausedFile leaves what a paused download leaves: part 0 on disk and the
// part list next to it.
func pausedFile(t *testing.T, path string) []byte {
	t.Helper()
	f, state, err := openTemp(path, partMedia(5))
	require.NoError(t, err)
	part := bytes.Repeat([]byte{7}, downloader.MaxPartSize)
	_, err = f.WriteAt(part, 0)
	require.NoError(t, err)
	state.add(0)
	require.NoError(t, f.Close())
	require.NoError(t, state.save(path))
	return part
}

func TestPausedPartsAreReopenedNotTruncated(t *testing.T) {
	path := filepath.Join(t.TempDir(), "video.mp4"+tempExt)
	part := pausedFile(t, path)

	f, state, err := openTemp(path, partMedia(5))
	require.NoError(t, err)
	defer f.Close()
	require.Equal(t, []int64{0}, state.parts())
	got := make([]byte, len(part))
	_, err = f.ReadAt(got, 0)
	require.NoError(t, err)
	require.Equal(t, part, got, "the part on disk must survive reopening")
	// Kept while continuing: it still describes the file after a crash.
	require.FileExists(t, path+partExt)
}

func TestPartListForAnotherFileStartsOver(t *testing.T) {
	for name, change := range map[string]func(path string){
		"other file": func(string) {},
		"short file": func(path string) { require.NoError(t, os.Truncate(path, 10)) },
		"bad list":   func(path string) { require.NoError(t, os.WriteFile(path+partExt, []byte("{"), 0o644)) },
	} {
		t.Run(name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "video.mp4"+tempExt)
			pausedFile(t, path)
			change(path)
			id := int64(5)
			if name == "other file" {
				id = 6
			}

			f, state, err := openTemp(path, partMedia(id))
			require.NoError(t, err)
			defer f.Close()
			require.Empty(t, state.parts())
			info, err := f.Stat()
			require.NoError(t, err)
			require.Zero(t, info.Size(), "a new download starts with an empty file")
			require.NoFileExists(t, path+partExt)
		})
	}
}

func doneWithError(t *testing.T, command string) (string, *iterElem) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "video.mp4"+tempExt)
	f, state, err := openTemp(path, partMedia(5))
	require.NoError(t, err)
	_, err = f.WriteAt(bytes.Repeat([]byte{1}, downloader.MaxPartSize), 0)
	require.NoError(t, err)
	state.add(0)

	ctx, bridge := desktopio.New(context.Background(), strings.NewReader(`{"command":"`+command+`"}`+"\n"), &bytes.Buffer{})
	t.Cleanup(bridge.Close)
	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("command was not read")
	}

	elem := &iterElem{id: 1, file: partMedia(5), to: f, partial: state}
	p := newProgress(ctx, prettyprogress.NewWriter(), &iter{mu: &sync.Mutex{}, finished: map[int]struct{}{}}, Options{})
	tracker := &prettyprogress.Tracker{Message: "video.mp4", Total: partTestSize}
	p.pw.AppendTracker(tracker)
	p.trackers.Store(elem.id, tracker)
	p.OnDone(elem, ctx.Err())
	return path, elem
}

func TestPauseKeepsPartsAndCancelRemovesThem(t *testing.T) {
	path, _ := doneWithError(t, "pause")
	require.FileExists(t, path)
	require.FileExists(t, path+partExt)
	parts, ok := readParts(path, locationKey(partMedia(5).InputFileLoc), partTestSize)
	require.True(t, ok)
	require.Equal(t, []int64{0}, parts)

	path, _ = doneWithError(t, "cancel")
	require.NoFileExists(t, path)
	require.NoFileExists(t, path+partExt)
}

// doneAfterFailure finishes a download whose part 0 is on disk with err,
// as the downloader reports a network or Telegram error.  With desktop set
// the run has a live bridge (no pause or cancel command); otherwise it runs
// like the command line.
func doneAfterFailure(t *testing.T, desktop bool, failure error) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "video.mp4"+tempExt)
	f, state, err := openTemp(path, partMedia(5))
	require.NoError(t, err)
	_, err = f.WriteAt(bytes.Repeat([]byte{1}, downloader.MaxPartSize), 0)
	require.NoError(t, err)
	state.add(0)

	ctx := context.Background()
	if desktop {
		var bridge *desktopio.Bridge
		ctx, bridge = desktopio.New(ctx, nil, &bytes.Buffer{})
		t.Cleanup(bridge.Close)
	}

	elem := &iterElem{id: 1, file: partMedia(5), to: f, partial: state}
	p := newProgress(ctx, prettyprogress.NewWriter(), &iter{mu: &sync.Mutex{}, finished: map[int]struct{}{}}, Options{})
	tracker := &prettyprogress.Tracker{Message: "video.mp4", Total: partTestSize}
	p.pw.AppendTracker(tracker)
	p.trackers.Store(elem.id, tracker)
	p.OnDone(elem, failure)
	return path
}

func TestDesktopFailureKeepsPartsForRetry(t *testing.T) {
	for name, failure := range map[string]error{
		// The file itself failed, for example a dropped connection.
		"network error": errors.New("rpc error: connection reset"),
		// Another file failed and the downloader stopped this one.
		"stopped by a sibling": context.Canceled,
	} {
		t.Run(name, func(t *testing.T) {
			path := doneAfterFailure(t, true, failure)
			require.FileExists(t, path)
			require.FileExists(t, path+partExt)
			parts, ok := readParts(path, locationKey(partMedia(5).InputFileLoc), partTestSize)
			require.True(t, ok)
			require.Equal(t, []int64{0}, parts)

			// The retry reopens the file and only fetches the missing parts.
			f, state, err := openTemp(path, partMedia(5))
			require.NoError(t, err)
			defer f.Close()
			require.Equal(t, []int64{0}, state.parts())
		})
	}
}

func TestOnlyALiveDesktopRunKeepsPartsAfterFailure(t *testing.T) {
	failure := errors.New("rpc error: connection reset")
	newCheck := func(ctx context.Context) *progress {
		return newProgress(ctx, prettyprogress.NewWriter(), &iter{mu: &sync.Mutex{}, finished: map[int]struct{}{}}, Options{})
	}

	// The command line keeps its original behavior: the temporary file goes.
	require.False(t, newCheck(context.Background()).keepAfterFailure(failure))

	ctx, bridge := desktopio.New(context.Background(), nil, &bytes.Buffer{})
	t.Cleanup(bridge.Close)
	check := newCheck(ctx)
	require.False(t, check.keepAfterFailure(nil), "a finished file has nothing to keep")
	require.True(t, check.keepAfterFailure(failure))
	require.True(t, check.keepAfterFailure(context.Canceled), "stopped because another file failed")

	// A user cancel stops the whole run; the desktop expects the files gone.
	cancelled, cancel := context.WithCancel(ctx)
	cancel()
	require.False(t, newCheck(cancelled).keepAfterFailure(context.Canceled))
}
