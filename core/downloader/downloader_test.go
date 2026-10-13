package downloader

import (
	"context"
	stderrors "errors"
	"io"
	"sync"
	"testing"
	"time"
)

type testElem struct {
	id int
}

func (e testElem) File() File {
	return nil
}

func (e testElem) To() io.WriterAt {
	return nil
}

func (e testElem) AsTakeout() bool {
	return false
}

type testIter struct {
	elems []Elem
	index int
}

func (i *testIter) Next(ctx context.Context) bool {
	if err := ctx.Err(); err != nil {
		return false
	}
	if i.index >= len(i.elems) {
		return false
	}
	i.index++
	return true
}

func (i *testIter) Value() Elem {
	return i.elems[i.index-1]
}

func (i *testIter) Err() error {
	return nil
}

type errorIter struct {
	elem Elem
	err  error
	done bool
}

func (i *errorIter) Next(context.Context) bool {
	if i.done {
		return false
	}
	i.done = true
	return true
}

func (i *errorIter) Value() Elem {
	return i.elem
}

func (i *errorIter) Err() error {
	return i.err
}

type doneRecord struct {
	elem Elem
	err  error
}

type testProgress struct {
	mu    sync.Mutex
	added []Elem
	done  []doneRecord
}

func (p *testProgress) OnAdd(elem Elem) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.added = append(p.added, elem)
}

func (p *testProgress) OnDownload(Elem, ProgressState) {}

func (p *testProgress) OnDone(elem Elem, err error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.done = append(p.done, doneRecord{elem: elem, err: err})
}

func TestDownloadForwardsFileFailureToOnDoneAndContinues(t *testing.T) {
	first := testElem{id: 1}
	second := testElem{id: 2}
	fileErr := stderrors.New("simulated file transfer failure")
	progress := &testProgress{}

	d := &Downloader{
		opts: Options{
			Iter:     &testIter{elems: []Elem{first, second}},
			Progress: progress,
		},
		downloadFn: func(_ context.Context, elem Elem) error {
			if elem.(testElem).id == first.id {
				return fileErr
			}
			return nil
		},
	}

	if err := d.Download(context.Background(), 2); err != nil {
		t.Fatalf("Download returned an error for a continued per-file failure: %v", err)
	}

	progress.mu.Lock()
	defer progress.mu.Unlock()
	if len(progress.added) != 2 {
		t.Fatalf("OnAdd calls = %d, want 2", len(progress.added))
	}
	if len(progress.done) != 2 {
		t.Fatalf("OnDone calls = %d, want 2", len(progress.done))
	}

	var firstErr, secondErr error
	for _, record := range progress.done {
		switch record.elem.(testElem).id {
		case first.id:
			firstErr = record.err
		case second.id:
			secondErr = record.err
		}
	}
	if !stderrors.Is(firstErr, fileErr) {
		t.Fatalf("first OnDone error = %v, want %v", firstErr, fileErr)
	}
	if secondErr != nil {
		t.Fatalf("second OnDone error = %v, want nil", secondErr)
	}
}

func TestDownloadWaitsForWorkersBeforeReturningIteratorError(t *testing.T) {
	elem := testElem{id: 3}
	iterErr := stderrors.New("simulated iterator failure")
	progress := &testProgress{}
	started := make(chan struct{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseWorker := func() { releaseOnce.Do(func() { close(release) }) }
	defer releaseWorker()

	d := &Downloader{
		opts: Options{
			Iter:     &errorIter{elem: elem, err: iterErr},
			Progress: progress,
		},
		downloadFn: func(ctx context.Context, _ Elem) error {
			close(started)
			select {
			case <-release:
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		},
	}

	result := make(chan error, 1)
	go func() { result <- d.Download(context.Background(), 1) }()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("download worker did not start")
	}

	select {
	case err := <-result:
		t.Fatalf("Download returned before active worker completed: %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	releaseWorker()

	select {
	case err := <-result:
		if !stderrors.Is(err, iterErr) {
			t.Fatalf("Download error = %v, want iterator error %v", err, iterErr)
		}
	case <-time.After(time.Second):
		t.Fatal("Download did not return after active worker was released")
	}

	progress.mu.Lock()
	defer progress.mu.Unlock()
	if len(progress.done) != 1 {
		t.Fatalf("OnDone calls = %d, want 1 before Download returned", len(progress.done))
	}
	if progress.done[0].err != nil {
		t.Fatalf("OnDone error = %v, want nil", progress.done[0].err)
	}
}
