package downloader

import (
	"context"
	"io"
	"sync"

	"github.com/go-faster/errors"
	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
	"go.uber.org/atomic"
	"golang.org/x/sync/errgroup"
)

// partGetter is the one call a resumed download needs; *tg.Client has it.
type partGetter interface {
	UploadGetFile(ctx context.Context, request *tg.UploadGetFileRequest) (tg.UploadFileClass, error)
}

// syncWriterAt serializes writes: the progress callbacks behind writeAt
// expect one writer at a time, as gotd's own download loop provides.
func syncWriterAt(w io.WriterAt) io.WriterAt {
	return &lockedWriterAt{w: w}
}

type lockedWriterAt struct {
	mu sync.Mutex
	w  io.WriterAt
}

func (l *lockedWriterAt) WriteAt(p []byte, off int64) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.w.WriteAt(p, off)
}

// partLength is the size of one part; only the last part may be shorter.
func partLength(size, index int64) int64 {
	if rest := size - index*MaxPartSize; rest < MaxPartSize {
		return rest
	}
	return MaxPartSize
}

// resumeParts downloads only the parts that an earlier, paused download did
// not write.  Each part is requested from the master DC the same way the
// regular downloader asks for it (precise, no CDN), so old and new parts come
// from the same source.  downloaded starts at the bytes already on disk so
// the progress continues where it stopped.
func resumeParts(ctx context.Context, client partGetter, file File, w io.WriterAt,
	done []int64, threads int, downloaded *atomic.Int64,
) error {
	size := file.Size()
	parts := (size + MaxPartSize - 1) / MaxPartSize
	have := make(map[int64]struct{}, len(done))
	already := int64(0)
	for _, index := range done {
		if index < 0 || index >= parts {
			continue
		}
		if _, dup := have[index]; dup {
			continue
		}
		have[index] = struct{}{}
		already += partLength(size, index)
	}
	downloaded.Store(already)

	if threads < 1 {
		threads = 1
	}
	g, gctx := errgroup.WithContext(ctx)
	missing := make(chan int64)
	g.Go(func() error {
		defer close(missing)
		for index := int64(0); index < parts; index++ {
			if _, ok := have[index]; ok {
				continue
			}
			select {
			case missing <- index:
			case <-gctx.Done():
				return gctx.Err()
			}
		}
		return nil
	})
	for i := 0; i < threads; i++ {
		g.Go(func() error {
			for index := range missing {
				data, err := getPart(gctx, client, file.Location(), index*MaxPartSize)
				if err != nil {
					return errors.Wrapf(err, "get part %d", index)
				}
				if want := partLength(size, index); int64(len(data)) != want {
					return errors.Errorf("part %d has %d bytes, want %d", index, len(data), want)
				}
				if _, err := w.WriteAt(data, index*MaxPartSize); err != nil {
					return errors.Wrapf(err, "write part %d", index)
				}
			}
			return nil
		})
	}
	return g.Wait()
}

// getPart fetches one part, waiting out FLOOD_WAIT and retrying timeouts
// like gotd's own reader.
func getPart(ctx context.Context, client partGetter, location tg.InputFileLocationClass, offset int64) ([]byte, error) {
	for {
		req := &tg.UploadGetFileRequest{
			Location: location,
			Offset:   offset,
			Limit:    MaxPartSize,
		}
		req.SetPrecise(true)

		r, err := client.UploadGetFile(ctx, req)
		if flood, err := tgerr.FloodWait(ctx, err); err != nil {
			if flood || tgerr.Is(err, tg.ErrTimeout) {
				continue
			}
			return nil, err
		}

		file, ok := r.(*tg.UploadFile)
		if !ok {
			return nil, errors.Errorf("unexpected response %T", r)
		}
		return file.Bytes, nil
	}
}
