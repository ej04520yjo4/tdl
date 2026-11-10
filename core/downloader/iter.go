package downloader

import (
	"context"
	"io"

	"github.com/gotd/td/tg"
)

type Iter interface {
	Next(ctx context.Context) bool
	Value() Elem
	Err() error
}

type Elem interface {
	File() File
	To() io.WriterAt

	AsTakeout() bool
}

// Resumable is implemented by elements that keep track of which parts are
// already on disk, so a paused download can continue instead of starting
// over.  Parts are MaxPartSize bytes and are numbered from the file start.
type Resumable interface {
	// DoneParts returns the parts written by an earlier, paused download.
	DoneParts() []int64
	// PartDone records that a part has been written to disk.
	PartDone(index int64)
}

type File interface {
	Location() tg.InputFileLocationClass
	Size() int64
	DC() int
}
