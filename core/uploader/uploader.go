package uploader

import (
	"context"
	"io"
	"time"

	"github.com/gabriel-vasile/mimetype"
	"github.com/go-faster/errors"
	"github.com/gotd/td/telegram/message"
	"github.com/gotd/td/telegram/message/entity"
	"github.com/gotd/td/telegram/message/styling"
	"github.com/gotd/td/telegram/uploader"
	"github.com/gotd/td/tg"
	"github.com/samber/lo"
	"golang.org/x/sync/errgroup"

	"github.com/iyear/tdl/core/util/fsutil"
	"github.com/iyear/tdl/core/util/mediautil"
)

// MaxPartSize refer to https://core.telegram.org/api/files#uploading-files
const MaxPartSize = 512 * 1024

type Uploader struct {
	opts Options

	// uploadFn is intentionally only used by package tests, so the scheduling
	// and completion path can be exercised without a live Telegram DC.
	uploadFn func(context.Context, Elem) error
}

type Options struct {
	Client   *tg.Client
	Threads  int
	Iter     Iter
	Progress Progress
}

func New(o Options) *Uploader {
	return &Uploader{opts: o}
}

func (u *Uploader) Upload(ctx context.Context, limit int) error {
	wg, wgctx := errgroup.WithContext(ctx)
	wg.SetLimit(limit)

	for u.opts.Iter.Next(wgctx) {
		elem := u.opts.Iter.Value()

		wg.Go(func() (rerr error) {
			u.opts.Progress.OnAdd(elem)
			var progressErr error
			defer func() { u.opts.Progress.OnDone(elem, progressErr) }()

			if err := u.uploadElem(wgctx, elem); err != nil {
				// Always pass the actual file error to the progress callback. A
				// nil error here would report a failed upload as done, and the
				// --rm option would then delete a local file that never
				// reached Telegram.
				progressErr = err

				// canceled by user, so we directly return error to stop all
				if errors.Is(err, context.Canceled) {
					return errors.Wrap(err, "upload")
				}

				// don't return error, just log it
			}

			return nil
		})
	}

	iterErr := u.opts.Iter.Err()
	// Wait for every started worker first so OnDone closes its file handles
	// before Upload returns to the caller.
	waitErr := wg.Wait()
	if iterErr != nil {
		return errors.Wrap(iterErr, "iter")
	}

	return waitErr
}

func (u *Uploader) uploadElem(ctx context.Context, elem Elem) error {
	if u.uploadFn != nil {
		return u.uploadFn(ctx, elem)
	}
	return u.upload(ctx, elem)
}

func (u *Uploader) upload(ctx context.Context, elem Elem) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}

	up := uploader.NewUploader(u.opts.Client).
		WithPartSize(MaxPartSize).
		WithThreads(u.opts.Threads).
		WithProgress(&wrapProcess{
			elem:    elem,
			process: u.opts.Progress,
		})

	f, err := up.Upload(ctx, uploader.NewUpload(elem.File().Name(), elem.File(), elem.File().Size()))
	if err != nil {
		return errors.Wrap(err, "upload file")
	}

	if _, err = elem.File().Seek(0, io.SeekStart); err != nil {
		return errors.Wrap(err, "seek file")
	}
	mime, err := mimetype.DetectReader(elem.File())
	if err != nil {
		return errors.Wrap(err, "detect mime")
	}

	// here convert underlying entities to formatters for message caption
	caption := styling.Custom(func(eb *entity.Builder) error {
		msg, entities := elem.Caption()
		eb.Format(msg, lo.Map(entities, func(item tg.MessageEntityClass, _ int) entity.Formatter {
			return func(_, _ int) tg.MessageEntityClass {
				return item
			}
		})...)
		return nil
	})

	doc := message.UploadedDocument(f, caption).MIME(mime.String()).Filename(elem.File().Name())
	// upload thumbnail TODO(iyear): maybe still unavailable
	if thumb, ok := elem.Thumb(); ok {
		if thumbFile, err := uploader.NewUploader(u.opts.Client).
			FromReader(ctx, thumb.Name(), thumb); err == nil {
			doc = doc.Thumb(thumbFile)
		}
	}

	var media message.MediaOption = doc

	switch {
	case mediautil.IsImage(mime.String()) && elem.AsPhoto():
		// webp should be uploaded as document
		if mime.String() == "image/webp" {
			break
		}
		// upload as photo
		media = message.UploadedPhoto(f, caption)
	case mediautil.IsVideo(mime.String()):
		// reset reader
		if _, err = elem.File().Seek(0, io.SeekStart); err != nil {
			return errors.Wrap(err, "seek file")
		}
		if dur, w, h, err := mediautil.GetMP4Info(elem.File()); err == nil {
			// #132. There may be some errors, but we can still upload the file
			media = doc.Video().
				Duration(time.Duration(dur)*time.Second).
				Resolution(w, h).
				SupportsStreaming()
		}
	case mediautil.IsAudio(mime.String()):
		media = doc.Audio().Title(fsutil.GetNameWithoutExt(elem.File().Name()))
	}

	_, err = message.NewSender(u.opts.Client).
		WithUploader(up).
		To(elem.To()).
		Reply(elem.Thread()).
		Media(ctx, media)
	if err != nil {
		return errors.Wrap(err, "send message")
	}

	return nil
}
