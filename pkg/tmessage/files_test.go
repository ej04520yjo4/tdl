package tmessage

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gotd/td/tg"
)

type countingReader struct {
	r io.Reader
	n int
}

func (c *countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.n += n
	return n, err
}

func exportJSON(head string, count int) string {
	var b strings.Builder
	b.WriteString("{" + head + `"messages":[`)
	for i := 1; i <= count; i++ {
		if i > 1 {
			b.WriteString(",")
		}
		fmt.Fprintf(&b, `{"id":%d,"type":"message","file":"f%d.mp4","text":"message %d"}`, i, i, i)
	}
	b.WriteString("]}")
	return b.String()
}

func TestReadChatHeaderStopsAtMessages(t *testing.T) {
	cases := []struct {
		name     string
		head     string
		id       int64
		peerType string
	}{
		{"tdl export", `"id":1234567890,"peer_type":"Channel",`, 1234567890, "channel"},
		// Telegram Desktop writes no peer_type.
		{"official export", `"name":"頻道","type":"public_channel","id":42,`, 42, ""},
		{"string id", `"id":" -1001234 ",`, -1001234, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			data := exportJSON(tc.head, 50000)
			r := &countingReader{r: strings.NewReader(data)}
			id, peerType, err := readChatHeader(r)
			if err != nil {
				t.Fatal(err)
			}
			if id != tc.id || peerType != tc.peerType {
				t.Fatalf("got (%d, %q), want (%d, %q)", id, peerType, tc.id, tc.peerType)
			}
			if r.n > 64*1024 {
				t.Fatalf("read %d of %d bytes; the messages array should not be read", r.n, len(data))
			}
		})
	}
}

func TestReadChatHeaderSkipsMessagesBeforeID(t *testing.T) {
	data := `{"messages":[{"id":7,"type":"message"},{"id":8,"nested":{"id":9}}],"id":5,"peer_type":"group"}`
	id, peerType, err := readChatHeader(strings.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if id != 5 || peerType != "group" {
		t.Fatalf("got (%d, %q), want (5, \"group\")", id, peerType)
	}
}

func TestReadChatHeaderRejectsNonObjects(t *testing.T) {
	for _, data := range []string{`[]`, `{"id":`, ``} {
		if _, _, err := readChatHeader(strings.NewReader(data)); err == nil {
			t.Fatalf("%q: expected an error", data)
		}
	}
}

// The header pass and the message pass share one file.  The streaming
// decoder used before kept reading after the header was found, so the
// second pass saw only part of a large export.
func TestHeaderThenCollectSeesEveryMessage(t *testing.T) {
	const count = 20000
	path := filepath.Join(t.TempDir(), "export.json")
	if err := os.WriteFile(path, []byte(exportJSON(`"id":1234567890,"peer_type":"channel",`, count)), 0o600); err != nil {
		t.Fatal(err)
	}
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()

	if _, _, err := readChatHeader(f); err != nil {
		t.Fatal(err)
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		t.Fatal(err)
	}
	dialog, err := collect(context.Background(), f, &tg.InputPeerEmpty{}, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(dialog.Messages) != count {
		t.Fatalf("collected %d messages, want %d", len(dialog.Messages), count)
	}
}

func TestCollectReportsTruncatedExport(t *testing.T) {
	data := exportJSON(`"id":1,`, 100)
	_, err := collect(context.Background(), strings.NewReader(data[:len(data)/2]), &tg.InputPeerEmpty{}, false)
	if err == nil {
		t.Fatal("expected a parse error for a truncated export")
	}
}
