package downloader

import (
	"context"

	"github.com/go-faster/errors"
	"github.com/gotd/td/telegram/downloader"
	"go.uber.org/zap"
	"golang.org/x/sync/errgroup"

	"github.com/iyear/tdl/core/dcpool"
	"github.com/iyear/tdl/core/logctx"
	"github.com/iyear/tdl/core/util/tutil"
)

// MaxPartSize refer to https://core.telegram.org/api/files#downloading-files
const MaxPartSize = 1024 * 1024

type Downloader struct {
	opts Options

	// downloadFn is intentionally only used by package tests. Keeping the
	// scheduling and completion path injectable lets tests exercise Download's
	// real error propagation without requiring a live Telegram DC.
	downloadFn func(context.Context, Elem) error
}

type Options struct {
	Pool     dcpool.Pool
	Threads  int
	Iter     Iter
	Progress Progress
}

func New(opts Options) *Downloader {
	return &Downloader{
		opts: opts,
	}
}

func (d *Downloader) Download(ctx context.Context, limit int) error {
	wg, wgctx := errgroup.WithContext(ctx)
	wg.SetLimit(limit)

	for d.opts.Iter.Next(wgctx) {
		elem := d.opts.Iter.Value()

		wg.Go(func() (rerr error) {
			d.opts.Progress.OnAdd(elem)
			var progressErr error
			defer func() { d.opts.Progress.OnDone(elem, progressErr) }()

			if err := d.downloadElem(wgctx, elem); err != nil {
				// Always pass the actual file error to the progress callback. A
				// nil error here would make the progress layer checkpoint and
				// report a file as complete even when its temporary file is
				// incomplete.
				progressErr = err

				// canceled by user, so we directly return error to stop all
				if errors.Is(err, context.Canceled) {
					return errors.Wrap(err, "download")
				}

				// don't return error, just log it
				logctx.
					From(ctx).
					Error("Download error",
						zap.Any("element", elem),
						zap.Error(err),
					)
			}

			return nil
		})
	}

	iterErr := d.opts.Iter.Err()
	waitErr := wg.Wait()
	if iterErr != nil {
		// Iterators can report context.Canceled after the caller stops the
		// iteration. Wait for all already-started workers first so their
		// OnDone callbacks close temporary files and update checkpoints before
		// Download returns to the caller.
		return errors.Wrap(iterErr, "iter")
	}

	return waitErr
}

func (d *Downloader) downloadElem(ctx context.Context, elem Elem) error {
	if d.downloadFn != nil {
		return d.downloadFn(ctx, elem)
	}
	return d.download(ctx, elem)
}

func (d *Downloader) download(ctx context.Context, elem Elem) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}

	logctx.From(ctx).Debug("Start download elem",
		zap.Any("elem", elem))

	client := d.opts.Pool.Client(ctx, elem.File().DC())
	if elem.AsTakeout() {
		client = d.opts.Pool.Takeout(ctx, elem.File().DC())
	}

	_, err := downloader.NewDownloader().WithPartSize(MaxPartSize).
		Download(client, elem.File().Location()).
		WithThreads(tutil.BestThreads(elem.File().Size(), d.opts.Threads)).
		Parallel(ctx, newWriteAt(elem, d.opts.Progress, MaxPartSize))
	if err != nil {
		return errors.Wrap(err, "download")
	}

	return nil
}
