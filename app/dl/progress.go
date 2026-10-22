package dl

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/fatih/color"
	"github.com/gabriel-vasile/mimetype"
	"github.com/go-faster/errors"
	pw "github.com/jedib0t/go-pretty/v6/progress"
	"go.uber.org/atomic"

	"github.com/iyear/tdl/core/downloader"
	"github.com/iyear/tdl/core/util/fsutil"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/prog"
	"github.com/iyear/tdl/pkg/utils"
)

type progress struct {
	pw       pw.Writer
	trackers *sync.Map // map[ID]*pw.Tracker
	opts     Options
	ctx      context.Context

	it *iter

	throttle *desktopio.Throttle
	failures *atomic.Int64
}

func newProgress(ctx context.Context, p pw.Writer, it *iter, opts Options) *progress {
	return &progress{
		pw:       p,
		trackers: &sync.Map{},
		opts:     opts,
		ctx:      ctx,
		it:       it,
		throttle: desktopio.NewThrottle(desktopio.ProgressInterval),
		failures: atomic.NewInt64(0),
	}
}

func (p *progress) OnAdd(elem downloader.Elem) {
	e := elem.(*iterElem)
	tracker := prog.AppendTracker(p.pw, utils.Byte.FormatBinaryBytes, p.processMessage(elem), elem.File().Size())
	p.trackers.Store(e.id, tracker)
	p.emitMachineState(e, 0, elem.File().Size(), "", p.machineFilename(e), "queued")
}

func (p *progress) OnDownload(elem downloader.Elem, state downloader.ProgressState) {
	tracker, ok := p.trackers.Load(elem.(*iterElem).id)
	if !ok {
		return
	}

	t := tracker.(*pw.Tracker)
	t.UpdateTotal(state.Total)
	t.SetValue(state.Downloaded)
	if desktopio.From(p.ctx) == nil {
		return
	}
	// WriteAt reports every part. Forwarding each one made the desktop client
	// persist and repaint dozens of times per second and file, and its pipe
	// back-pressure throttled the download itself.
	e := elem.(*iterElem)
	rate, ok := p.throttle.Allow(fmt.Sprintf("%d", e.id), state.Downloaded)
	if !ok {
		return
	}
	speed := ""
	if rate > 0 {
		speed = utils.Byte.FormatBinaryBytes(rate) + "/s"
	}
	p.emitMachineState(e, state.Downloaded, state.Total, speed, p.machineFilename(e), "downloading")
}

func (p *progress) OnDone(elem downloader.Elem, err error) {
	e := elem.(*iterElem)
	p.throttle.Forget(fmt.Sprintf("%d", e.id))

	tracker, ok := p.trackers.Load(e.id)
	if !ok {
		return
	}
	t := tracker.(*pw.Tracker)

	if err := e.to.Close(); err != nil {
		p.fail(t, elem, errors.Wrap(err, "close file"))
		p.emitMachineError(e, p.machineFilename(e), errors.Wrap(err, "close file"))
		return
	}

	if err != nil {
		if !errors.Is(err, context.Canceled) { // don't report user cancel
			failure := errors.Wrap(err, "progress")
			p.fail(t, elem, failure)
			p.emitMachineError(e, p.machineFilename(elem), errors.Wrap(failure, "下載失敗"))
		}
		_ = os.Remove(e.to.Name()) // just try to remove temp file, ignore error
		return
	}

	// The logical position is the durable resume checkpoint.  Mark it only
	// after the temporary file has been renamed and its final metadata has
	// been applied.  Otherwise a rename/Chtimes failure would be reported to
	// the GUI while --continue later skipped the failed file forever.
	finalPath, postErr := p.donePost(e)
	if postErr != nil {
		failure := errors.Wrap(postErr, "post file")
		p.fail(t, elem, failure)
		p.emitMachineError(e, filepath.Base(finalPath), failure)
		return
	}
	p.it.Finish(e.logicalPos)

	// Report 100% only once post-processing has succeeded.  The progress
	// callback may still report the byte transfer reaching its total before
	// OnDone, but this final event is the completion signal consumed by the
	// desktop frontend.
	p.emitMachineState(e, e.file.Size, e.file.Size, "", filepath.Base(finalPath), "completed")
}

func (p *progress) emitMachineState(elem *iterElem, current, total int64, speed, filename, state string) {
	if desktopio.From(p.ctx) == nil {
		return
	}
	percent := float64(0)
	if total > 0 {
		percent = float64(current) * 100 / float64(total)
		if percent > 100 {
			percent = 100
		}
	}
	_ = desktopio.Emit(p.ctx, desktopio.Event{
		Event:    "progress",
		Percent:  percent,
		Current:  current,
		Total:    total,
		Speed:    speed,
		Filename: filename,
		FileID:   fmt.Sprintf("%d", elem.logicalPos),
		State:    state,
		// The folder lets the desktop open where the files really are; with
		// per-chat folders that is below the chosen download directory.
		Path: p.machineFolder(elem),
	})
}

func (p *progress) machineFolder(elem *iterElem) string {
	folder := filepath.Dir(elem.to.Name())
	if abs, err := filepath.Abs(folder); err == nil {
		return abs
	}
	return folder
}

func (p *progress) machineFilename(elem downloader.Elem) string {
	e := elem.(*iterElem)
	return strings.TrimSuffix(filepath.Base(e.to.Name()), tempExt)
}

func (p *progress) emitMachineError(elem *iterElem, filename string, err error) {
	if desktopio.From(p.ctx) == nil {
		return
	}
	if strings.TrimSpace(filename) == "" || filename == "." {
		filename = "未知檔案"
	}
	_ = desktopio.Emit(p.ctx, desktopio.Event{
		Event:   "error",
		Message: desktopio.SafeError(errors.Wrapf(err, "檔案 %s 處理失敗", filename)),
		FileID:  fmt.Sprintf("%d", elem.logicalPos),
		State:   "failed",
	})
}

func (p *progress) donePost(elem *iterElem) (string, error) {
	newfile := strings.TrimSuffix(filepath.Base(elem.to.Name()), tempExt)
	newpath := filepath.Join(filepath.Dir(elem.to.Name()), newfile)

	if p.opts.RewriteExt {
		mime, err := mimetype.DetectFile(elem.to.Name())
		if err != nil {
			return newpath, errors.Wrap(err, "detect mime")
		}
		ext := mime.Extension()
		if ext != "" && (filepath.Ext(newfile) != ext) {
			newfile = fsutil.GetNameWithoutExt(newfile) + ext
		}
		newpath = filepath.Join(filepath.Dir(elem.to.Name()), newfile)
	}

	// Apply metadata while the file still has its temporary name. If this
	// fails, the temporary file remains available for diagnosis/retry and the
	// logical resume position is not accidentally associated with a final file.
	if elem.file.Date > 0 {
		fileTime := time.Unix(elem.file.Date, 0)
		if err := os.Chtimes(elem.to.Name(), fileTime, fileTime); err != nil {
			return newpath, errors.Wrap(err, "set file time")
		}
	}

	if err := os.Rename(elem.to.Name(), newpath); err != nil {
		return newpath, errors.Wrap(err, "rename file")
	}

	return newpath, nil
}

func (p *progress) fail(t *pw.Tracker, elem downloader.Elem, err error) {
	p.failures.Inc()
	if desktopio.From(p.ctx) == nil {
		p.pw.Log(color.RedString("%s error: %s", p.elemString(elem), err.Error()))
	}
	t.MarkAsErrored()
}

func (p *progress) Failures() int64 {
	return p.failures.Load()
}

func (p *progress) processMessage(elem downloader.Elem) string {
	return p.elemString(elem)
}

func (p *progress) elemString(elem downloader.Elem) string {
	e := elem.(*iterElem)
	return fmt.Sprintf("%s(%d):%d -> %s",
		e.from.VisibleName(),
		e.from.ID(),
		e.fromMsg.ID,
		strings.TrimSuffix(e.to.Name(), tempExt))
}
