package forward

import (
	"context"
	"fmt"
	"os"
	"reflect"
	"strings"

	"github.com/expr-lang/expr"
	"github.com/expr-lang/expr/vm"
	"github.com/go-faster/errors"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/peers"
	pw "github.com/jedib0t/go-pretty/v6/progress"
	"github.com/spf13/viper"
	"go.uber.org/multierr"

	"github.com/iyear/tdl/app/internal/tctx"
	"github.com/iyear/tdl/core/dcpool"
	"github.com/iyear/tdl/core/forwarder"
	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/core/tclient"
	"github.com/iyear/tdl/core/util/tutil"
	"github.com/iyear/tdl/pkg/consts"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/prog"
	"github.com/iyear/tdl/pkg/texpr"
	"github.com/iyear/tdl/pkg/tmessage"
)

type Options struct {
	From []string
	To   string
	// TopicID is the constant Telegram forum topic destination.  Zero keeps
	// the historical expression routing behaviour; positive values override
	// the per-message thread only after the destination has been validated as
	// a typed channel or group peer.
	TopicID int
	Edit    string
	Mode    forwarder.Mode
	Silent  bool
	DryRun  bool
	Single  bool
	Desc    bool
}

const maxTopicID = int64(1<<31 - 1)

// Validate checks the local --topic contract before a Telegram connection is
// opened.  A topic is deliberately restricted to a concrete typed peer so a
// batch manifest cannot accidentally route to a different peer through an
// expression result.
func (o Options) Validate() error {
	topic := int64(o.TopicID)
	if topic < 0 || topic > maxTopicID {
		return fmt.Errorf("invalid --topic %d: must be between 0 and %d", o.TopicID, maxTopicID)
	}
	if topic == 0 {
		return nil
	}

	destination := strings.TrimSpace(o.To)
	if destination == "" {
		return fmt.Errorf("error flags: --to should be set when --topic is set")
	}
	kind, _, ok := tutil.ParseTypedPeer(destination)
	if !ok || (kind != "channel" && kind != "chat") {
		return fmt.Errorf("error flags: --topic requires a typed destination channel:<id> or chat:<id>")
	}
	return nil
}

func Run(ctx context.Context, c *telegram.Client, kvd storage.Storage, opts Options) (rerr error) {
	if err := opts.Validate(); err != nil {
		return err
	}
	if opts.To == "-" || opts.Edit == "-" {
		fg := texpr.NewFieldsGetter(nil)

		fields, err := fg.Walk(exprEnv(nil, nil))
		if err != nil {
			return fmt.Errorf("failed to walk fields: %w", err)
		}

		fmt.Print(fg.Sprint(fields, true))
		return nil
	}

	ctx = tctx.WithKV(ctx, kvd)

	pool := dcpool.NewPool(c,
		int64(viper.GetInt(consts.FlagPoolSize)),
		tclient.NewDefaultMiddlewares(ctx, viper.GetDuration(consts.FlagReconnectTimeout))...)
	defer multierr.AppendInvoke(&rerr, multierr.Close(pool))

	ctx = tctx.WithPool(ctx, pool)

	dialogs, err := collectDialogs(ctx, opts.From, opts.Desc)
	if err != nil {
		return errors.Wrap(err, "collect dialogs")
	}

	manager := peers.Options{Storage: storage.NewPeers(kvd)}.Build(pool.Default(ctx))

	to, err := resolveDest(ctx, manager, strings.TrimSpace(opts.To))
	if err != nil {
		return errors.Wrap(err, "resolve dest peer")
	}

	edit, err := resolveEdit(opts.Edit)
	if err != nil {
		return errors.Wrap(err, "resolve edit")
	}

	gui := desktopio.From(ctx) != nil
	total := totalMessages(dialogs)
	if total == 0 {
		if gui {
			return errors.New("沒有找到可轉傳的訊息")
		}
		return errors.New("no messages found")
	}
	if gui {
		_ = desktopio.Emit(ctx, desktopio.Event{
			Event:   "status",
			Kind:    "forward",
			Count:   int64(total),
			Message: fmt.Sprintf("共 %d 則訊息等待轉傳", total),
		})
	}

	fwProgress := prog.New(pw.FormatNumber)
	fwProgress.SetNumTrackersExpected(total)
	if !gui && !viper.GetBool(consts.FlagDisableProgressPS) {
		prog.EnablePS(ctx, fwProgress)
	}

	machineProgress := newProgress(ctx, fwProgress)
	fw := forwarder.New(forwarder.Options{
		Pool: pool,
		Iter: newIter(iterOptions{
			manager: manager,
			pool:    pool,
			to:      to,
			topic:   opts.TopicID,
			edit:    edit,
			dialogs: dialogs,
			mode:    opts.Mode,
			silent:  opts.Silent,
			dryRun:  opts.DryRun,
			// Batch callers pass --single so grouped album expansion cannot
			// pull messages outside the scanned date/media/topic manifest.
			grouped: !opts.Single,
			delay:   viper.GetDuration(consts.FlagDelay),
		}),
		Progress: machineProgress,
		Threads:  viper.GetInt(consts.FlagThreads),
	})

	if !gui {
		go fwProgress.Render()
		defer prog.Wait(ctx, fwProgress)
	}

	err = fw.Forward(ctx)
	if err == nil && gui && machineProgress.Failures() > 0 {
		return errors.New("有訊息轉傳失敗，請查看錯誤訊息後重試")
	}
	return err
}

func collectDialogs(ctx context.Context, input []string, desc bool) ([]*tmessage.Dialog, error) {
	var dialogs []*tmessage.Dialog

	for _, p := range input {
		var (
			d   []*tmessage.Dialog
			err error
		)

		switch {
		case strings.HasPrefix(p, "http"):
			d, err = tmessage.Parse(tmessage.FromURL(ctx, tctx.Pool(ctx), tctx.KV(ctx), []string{p}))
			if err != nil {
				return nil, errors.Wrap(err, "parse from url")
			}
		default:
			d, err = tmessage.Parse(tmessage.FromFile(ctx, tctx.Pool(ctx), tctx.KV(ctx), []string{p}, false))
			if err != nil {
				return nil, errors.Wrap(err, "parse from file")
			}
		}

		if desc {
			for _, dd := range d {
				if dd == nil {
					continue
				}
				for i, j := 0, len(dd.Messages)-1; i < j; i, j = i+1, j-1 {
					dd.Messages[i], dd.Messages[j] = dd.Messages[j], dd.Messages[i]
				}
			}
		}

		dialogs = append(dialogs, d...)
	}

	return dialogs, nil
}

// resolveDest parses the input string and returns a vm.Program. It can be a CHAT, a text or a file based on expression engine.
func resolveDest(ctx context.Context, manager *peers.Manager, input string) (*vm.Program, error) {
	compile := func(i string) (*vm.Program, error) {
		// we pass empty peer and message to enable type checking
		return expr.Compile(i, expr.Env(exprEnv(nil, nil)))
	}

	// default
	if input == "" {
		return compile(`""`)
	}

	// file
	if exp, err := os.ReadFile(input); err == nil {
		return compile(string(exp))
	}

	// chat
	if _, err := tutil.GetInputPeer(ctx, manager, input); err == nil {
		// convert to const string
		return compile(fmt.Sprintf(`"%s"`, input))
	}

	// text
	return compile(input)
}

// resolveEdit returns nil if input is empty, otherwise it returns a vm.Program. It can be a text or a file based on expression engine.
func resolveEdit(input string) (*vm.Program, error) {
	compile := func(i string) (*vm.Program, error) {
		// we pass empty peer and message to enable type checking
		return expr.Compile(i, expr.Env(exprEnv(nil, nil)), expr.AsKind(reflect.String))
	}

	// no edit, nil program
	if input == "" {
		return nil, nil
	}

	// file
	if exp, err := os.ReadFile(input); err == nil {
		return compile(string(exp))
	}

	// text
	return compile(input)
}

func totalMessages(dialogs []*tmessage.Dialog) int {
	var total int
	for _, d := range dialogs {
		if d == nil {
			continue
		}
		total += len(d.Messages)
	}
	return total
}
