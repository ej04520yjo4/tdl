package forward

import (
	"context"
	"fmt"
	"strings"

	"github.com/fatih/color"
	"github.com/go-faster/errors"
	pw "github.com/jedib0t/go-pretty/v6/progress"
	"github.com/mattn/go-runewidth"
	"go.uber.org/atomic"

	"github.com/iyear/tdl/core/forwarder"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/prog"
	"github.com/iyear/tdl/pkg/utils"
)

type progress struct {
	pw       pw.Writer
	trackers map[tuple]*pw.Tracker // TODO(iyear): concurrent map
	elemName map[int64]string
	ctx      context.Context

	throttle *desktopio.Throttle
	failures *atomic.Int64
}

type tuple struct {
	from int64
	msg  int
	to   int64
}

func newProgress(ctx context.Context, p pw.Writer) *progress {
	return &progress{
		pw:       p,
		trackers: make(map[tuple]*pw.Tracker),
		elemName: make(map[int64]string),
		ctx:      ctx,
		throttle: desktopio.NewThrottle(desktopio.ProgressInterval),
		failures: atomic.NewInt64(0),
	}
}

func (p *progress) OnAdd(elem forwarder.Elem) {
	tracker := prog.AppendTracker(p.pw, pw.FormatNumber, p.processMessage(elem, false), 1)
	p.trackers[p.tuple(elem)] = tracker
	p.emitMachineState(elem, 0, 0, "queued")
}

func (p *progress) OnClone(elem forwarder.Elem, state forwarder.ProgressState) {
	tracker, ok := p.trackers[p.tuple(elem)]
	if !ok {
		return
	}

	// display re-upload transfer info
	tracker.Units.Formatter = utils.Byte.FormatBinaryBytes
	tracker.UpdateMessage(p.processMessage(elem, true))
	tracker.UpdateTotal(state.Total)
	tracker.SetValue(state.Done)
	if desktopio.From(p.ctx) == nil {
		return
	}
	if _, ok := p.throttle.Allow(p.machineID(elem), state.Done); ok {
		p.emitMachineState(elem, state.Done, state.Total, "cloning")
	}
}

func (p *progress) OnDone(elem forwarder.Elem, err error) {
	p.throttle.Forget(p.machineID(elem))

	tracker, ok := p.trackers[p.tuple(elem)]
	if !ok {
		return
	}

	if err != nil {
		if errors.Is(err, context.Canceled) { // don't report user cancel
			return
		}
		p.failures.Inc()
		if desktopio.From(p.ctx) == nil {
			p.pw.Log(color.RedString("%s error: %s", p.metaString(elem), err.Error()))
		} else {
			_ = desktopio.Emit(p.ctx, desktopio.Event{
				Event:   "error",
				Message: desktopio.SafeError(errors.Wrapf(err, "訊息 %d 轉傳失敗", elem.Msg().ID)),
				FileID:  p.machineID(elem),
				State:   "failed",
			})
		}
		tracker.MarkAsErrored()
		return
	}

	if tracker.Total == 1 {
		tracker.Increment(1)
	}
	tracker.MarkAsDone()
	p.emitMachineState(elem, 1, 1, "completed")
}

func (p *progress) Failures() int64 {
	return p.failures.Load()
}

// machineID identifies one forwarded message for the desktop client.
func (p *progress) machineID(elem forwarder.Elem) string {
	return fmt.Sprintf("%d:%d:%d", elem.From().ID(), elem.Msg().ID, elem.To().ID())
}

func (p *progress) emitMachineState(elem forwarder.Elem, current, total int64, state string) {
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
		Event:   "progress",
		Percent: percent,
		Current: current,
		Total:   total,
		Filename: fmt.Sprintf("%s #%d → %s",
			elem.From().VisibleName(), elem.Msg().ID, elem.To().VisibleName()),
		FileID: p.machineID(elem),
		State:  state,
	})
}

func (p *progress) tuple(elem forwarder.Elem) tuple {
	return tuple{
		from: elem.From().ID(),
		msg:  elem.Msg().ID,
		to:   elem.To().ID(),
	}
}

func (p *progress) processMessage(elem forwarder.Elem, clone bool) string {
	b := &strings.Builder{}

	b.WriteString(p.metaString(elem))
	if clone {
		b.WriteString(" [clone]")
	}

	return b.String()
}

func (p *progress) metaString(elem forwarder.Elem) string {
	// TODO(iyear): better responsive name
	if _, ok := p.elemName[elem.From().ID()]; !ok {
		p.elemName[elem.From().ID()] = runewidth.Truncate(elem.From().VisibleName(), 15, "...")
	}
	if _, ok := p.elemName[elem.To().ID()]; !ok {
		p.elemName[elem.To().ID()] = runewidth.Truncate(elem.To().VisibleName(), 15, "...")
	}

	return fmt.Sprintf("%s(%d):%d -> %s(%d)",
		p.elemName[elem.From().ID()],
		elem.From().ID(),
		elem.Msg().ID,
		p.elemName[elem.To().ID()],
		elem.To().ID())
}
