package up

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"

	"github.com/fatih/color"
	"github.com/go-faster/errors"
	pw "github.com/jedib0t/go-pretty/v6/progress"
	"go.uber.org/atomic"

	"github.com/iyear/tdl/core/uploader"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/prog"
	"github.com/iyear/tdl/pkg/utils"
)

type progress struct {
	pw       pw.Writer
	trackers *sync.Map // map[tuple]*pw.Tracker
	ctx      context.Context

	throttle *desktopio.Throttle
	failures *atomic.Int64
}

type tuple struct {
	name string
	to   int64
}

func newProgress(ctx context.Context, p pw.Writer) *progress {
	return &progress{
		pw:       p,
		trackers: &sync.Map{},
		ctx:      ctx,
		throttle: desktopio.NewThrottle(desktopio.ProgressInterval),
		failures: atomic.NewInt64(0),
	}
}

func (p *progress) OnAdd(elem uploader.Elem) {
	tracker := prog.AppendTracker(p.pw, utils.Byte.FormatBinaryBytes, p.processMessage(elem), elem.File().Size())
	p.trackers.Store(p.tuple(elem), tracker)
	p.emitMachineState(elem.(*iterElem), 0, elem.File().Size(), "", "queued")
}

func (p *progress) OnUpload(elem uploader.Elem, state uploader.ProgressState) {
	tracker, ok := p.trackers.Load(p.tuple(elem))
	if !ok {
		return
	}

	t := tracker.(*pw.Tracker)
	t.UpdateTotal(state.Total)
	t.SetValue(state.Uploaded)
	if desktopio.From(p.ctx) == nil {
		return
	}
	e := elem.(*iterElem)
	rate, ok := p.throttle.Allow(e.file.File.Name(), state.Uploaded)
	if !ok {
		return
	}
	speed := ""
	if rate > 0 {
		speed = utils.Byte.FormatBinaryBytes(rate) + "/s"
	}
	p.emitMachineState(e, state.Uploaded, state.Total, speed, "uploading")
}

func (p *progress) OnDone(elem uploader.Elem, err error) {
	e := elem.(*iterElem)
	p.throttle.Forget(e.file.File.Name())

	tracker, ok := p.trackers.Load(p.tuple(elem))
	if !ok {
		return
	}
	t := tracker.(*pw.Tracker)

	if err := p.closeFile(e); err != nil {
		p.fail(t, elem, errors.Wrap(err, "close file"))
		return
	}

	if err != nil {
		if !errors.Is(err, context.Canceled) { // don't report user cancel
			p.fail(t, elem, errors.Wrap(err, "progress"))
		}
		return
	}

	if e.remove {
		if err := os.Remove(e.file.File.Name()); err != nil {
			p.fail(t, elem, errors.Wrap(err, "remove file"))
			return
		}
	}

	p.emitMachineState(e, e.file.Size(), e.file.Size(), "", "completed")
}

func (p *progress) closeFile(e *iterElem) error {
	if err := e.file.Close(); err != nil {
		return errors.Wrap(err, "close file")
	}

	if e.thumb != nil {
		if err := e.thumb.Close(); err != nil {
			return errors.Wrap(err, "close thumb")
		}
	}

	return nil
}

func (p *progress) fail(t *pw.Tracker, elem uploader.Elem, err error) {
	p.failures.Inc()
	if desktopio.From(p.ctx) == nil {
		p.pw.Log(color.RedString("%s error: %s", p.elemString(elem), err.Error()))
	} else {
		e := elem.(*iterElem)
		_ = desktopio.Emit(p.ctx, desktopio.Event{
			Event:   "error",
			Message: desktopio.SafeError(errors.Wrapf(err, "檔案 %s 上傳失敗", filepath.Base(e.file.File.Name()))),
			FileID:  e.file.File.Name(),
			State:   "failed",
		})
	}
	t.MarkAsErrored()
}

func (p *progress) Failures() int64 {
	return p.failures.Load()
}

// emitMachineState reports one local file to the desktop client. The path is
// the stable identity: one upload run never contains the same file twice.
func (p *progress) emitMachineState(e *iterElem, current, total int64, speed, state string) {
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
		Filename: filepath.Base(e.file.File.Name()),
		FileID:   e.file.File.Name(),
		State:    state,
	})
}

func (p *progress) tuple(elem uploader.Elem) tuple {
	return tuple{elem.(*iterElem).file.File.Name(), elem.(*iterElem).to.ID()}
}

func (p *progress) processMessage(elem uploader.Elem) string {
	return p.elemString(elem)
}

func (p *progress) elemString(elem uploader.Elem) string {
	e := elem.(*iterElem)
	return fmt.Sprintf("%s -> %s(%d)", e.file.File.Name(), e.to.VisibleName(), e.to.ID())
}
