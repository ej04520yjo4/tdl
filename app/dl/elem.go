package dl

import (
	"io"
	"os"

	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"

	"github.com/iyear/tdl/core/downloader"
	"github.com/iyear/tdl/core/tmedia"
)

type iterElem struct {
	id         int // tracker id for progress tracking
	logicalPos int // logical position for resume/finished tracking

	from    peers.Peer
	fromMsg *tg.Message
	file    *tmedia.Media

	to *os.File
	// partial tracks the parts on disk, so a pause can keep them.
	partial *partState

	opts Options
}

// DoneParts lists the parts a paused download already wrote.
func (i *iterElem) DoneParts() []int64 { return i.partial.parts() }

// PartDone records a part written to the temporary file.
func (i *iterElem) PartDone(index int64) { i.partial.add(index) }

func (i *iterElem) File() downloader.File { return i }

func (i *iterElem) To() io.WriterAt { return i.to }

func (i *iterElem) AsTakeout() bool { return i.opts.Takeout }

func (i *iterElem) Location() tg.InputFileLocationClass { return i.file.InputFileLoc }

func (i *iterElem) Name() string { return i.file.Name }

func (i *iterElem) Size() int64 { return i.file.Size }

func (i *iterElem) DC() int { return i.file.DC }
