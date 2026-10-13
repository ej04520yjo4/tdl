package uploader

import (
	"context"
	stderrors "errors"
	"sync"
	"testing"

	"github.com/gotd/td/tg"
)

type testElem struct {
	id int
}

func (e testElem) File() File                                 { return nil }
func (e testElem) Thumb() (File, bool)                        { return nil, false }
func (e testElem) Caption() (string, []tg.MessageEntityClass) { return "", nil }
func (e testElem) To() tg.InputPeerClass                      { return nil }
func (e testElem) Thread() int                                { return 0 }
func (e testElem) AsPhoto() bool                              { return false }

type testIter struct {
	elems []Elem
	index int
}

func (i *testIter) Next(ctx context.Context) bool {
	if ctx.Err() != nil || i.index >= len(i.elems) {
		return false
	}
	i.index++
	return true
}

func (i *testIter) Value() Elem { return i.elems[i.index-1] }

func (i *testIter) Err() error { return nil }

type testProgress struct {
	mu   sync.Mutex
	done map[int]error
}

func (p *testProgress) OnAdd(Elem) {}

func (p *testProgress) OnUpload(Elem, ProgressState) {}

func (p *testProgress) OnDone(elem Elem, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.done[elem.(testElem).id] = err
}

// A failed upload must reach OnDone as a failure. The progress layer deletes
// the local file on a nil error when --rm is set, so reporting nil here would
// remove a file that never reached Telegram.
func TestUploadForwardsFileFailureToOnDoneAndContinues(t *testing.T) {
	first := testElem{id: 1}
	second := testElem{id: 2}
	fileErr := stderrors.New("simulated upload failure")
	progress := &testProgress{done: make(map[int]error)}

	u := &Uploader{
		opts: Options{
			Iter:     &testIter{elems: []Elem{first, second}},
			Progress: progress,
		},
		uploadFn: func(_ context.Context, elem Elem) error {
			if elem.(testElem).id == first.id {
				return fileErr
			}
			return nil
		},
	}

	if err := u.Upload(context.Background(), 2); err != nil {
		t.Fatalf("Upload returned an error for a continued per-file failure: %v", err)
	}

	progress.mu.Lock()
	defer progress.mu.Unlock()
	if len(progress.done) != 2 {
		t.Fatalf("OnDone calls = %d, want 2", len(progress.done))
	}
	if !stderrors.Is(progress.done[first.id], fileErr) {
		t.Fatalf("first OnDone error = %v, want %v", progress.done[first.id], fileErr)
	}
	if progress.done[second.id] != nil {
		t.Fatalf("second OnDone error = %v, want nil", progress.done[second.id])
	}
}
