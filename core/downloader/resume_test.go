package downloader

import (
	"bytes"
	"context"
	"io"
	"strings"
	"sync"
	"testing"

	"github.com/gotd/td/tg"
	"go.uber.org/atomic"
)

type partsFile struct{ size int64 }

func (f partsFile) Location() tg.InputFileLocationClass { return &tg.InputDocumentFileLocation{ID: 7} }
func (f partsFile) Size() int64                         { return f.size }
func (f partsFile) DC() int                             { return 2 }

// fakeParts serves a deterministic file and records which offsets it sent.
type fakeParts struct {
	content []byte
	short   int64 // offset answered with one byte too few, or -1

	mu      sync.Mutex
	offsets []int64
}

func (f *fakeParts) UploadGetFile(_ context.Context, req *tg.UploadGetFileRequest) (tg.UploadFileClass, error) {
	f.mu.Lock()
	f.offsets = append(f.offsets, req.Offset)
	f.mu.Unlock()
	end := req.Offset + int64(req.Limit)
	if end > int64(len(f.content)) {
		end = int64(len(f.content))
	}
	data := f.content[req.Offset:end]
	if req.Offset == f.short {
		data = data[:len(data)-1]
	}
	return &tg.UploadFile{Bytes: data}, nil
}

type memoryFile struct {
	mu   sync.Mutex
	data []byte
}

func (m *memoryFile) WriteAt(p []byte, off int64) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if need := off + int64(len(p)); need > int64(len(m.data)) {
		m.data = append(m.data, make([]byte, need-int64(len(m.data)))...)
	}
	copy(m.data[off:], p)
	return len(p), nil
}

func testContent(size int) []byte {
	content := make([]byte, size)
	for i := range content {
		content[i] = byte(i*7 + i/MaxPartSize)
	}
	return content
}

func TestResumeFetchesOnlyMissingParts(t *testing.T) {
	size := 2*MaxPartSize + 300*1024
	content := testContent(size)
	server := &fakeParts{content: content, short: -1}

	// The paused run wrote part 1; its bytes are already on disk.
	disk := &memoryFile{data: make([]byte, size)}
	copy(disk.data[MaxPartSize:2*MaxPartSize], content[MaxPartSize:2*MaxPartSize])

	downloaded := atomic.NewInt64(0)
	err := resumeParts(context.Background(), server, partsFile{size: int64(size)}, disk, []int64{1, 1, 9}, 3, downloaded)
	if err != nil {
		t.Fatalf("resume: %v", err)
	}
	if !bytes.Equal(disk.data, content) {
		t.Fatal("resumed file differs from the original")
	}
	if len(server.offsets) != 2 {
		t.Fatalf("fetched offsets %v, want only parts 0 and 2", server.offsets)
	}
	for _, offset := range server.offsets {
		if offset == MaxPartSize {
			t.Fatal("re-downloaded a part that was already on disk")
		}
	}
	if downloaded.Load() != MaxPartSize {
		t.Fatalf("progress started at %d, want the %d bytes already on disk", downloaded.Load(), MaxPartSize)
	}
}

func TestResumeRejectsAShortPart(t *testing.T) {
	size := 2 * MaxPartSize
	server := &fakeParts{content: testContent(size), short: MaxPartSize}
	disk := &memoryFile{}

	err := resumeParts(context.Background(), server, partsFile{size: int64(size)}, disk, []int64{0}, 1, atomic.NewInt64(0))
	if err == nil || !strings.Contains(err.Error(), "want") {
		t.Fatalf("short part was accepted: %v", err)
	}
}

func TestWriteAtReportsWholeParts(t *testing.T) {
	var got []int64
	w := newWriteAt(testWriteElem{to: &memoryFile{}}, noProgress{}, MaxPartSize)
	w.partDone = func(index int64) { got = append(got, index) }

	if _, err := w.WriteAt(make([]byte, MaxPartSize), 2*MaxPartSize); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0] != 2 {
		t.Fatalf("parts reported %v, want [2]", got)
	}
}

type testWriteElem struct{ to *memoryFile }

func (e testWriteElem) File() File      { return partsFile{size: 3 * MaxPartSize} }
func (e testWriteElem) To() io.WriterAt { return e.to }
func (e testWriteElem) AsTakeout() bool { return false }

type noProgress struct{}

func (noProgress) OnAdd(Elem)                     {}
func (noProgress) OnDownload(Elem, ProgressState) {}
func (noProgress) OnDone(Elem, error)             {}
