// Package desktopio provides the line based protocol used by the optional
// desktop front end.  The regular command line application never constructs a
// Bridge, so it does not read stdin or change its output in normal use.
package desktopio

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"regexp"
	"strings"
	"sync"
)

// Prefix marks machine-readable lines in stdout.
const Prefix = "@@TDL_GUI@@"

const prefix = Prefix

// Bridge is a small JSON-lines transport.  Every event written by Bridge is
// prefixed so a desktop client can safely ignore legacy human-readable output
// emitted by commands which have not yet been converted to machine events.
type Bridge struct {
	out io.Writer
	in  io.Reader

	responses chan string
	cancel    context.CancelFunc

	writeMu sync.Mutex
	closeMu sync.Once
	done    chan struct{}
}

// Event is the common envelope used by the desktop protocol.  Fields which do
// not apply to an event are omitted, keeping the wire format small and stable.
type Event struct {
	Event    string `json:"event"`
	Kind     string `json:"kind,omitempty"`
	Message  string `json:"message,omitempty"`
	Stage    string `json:"stage,omitempty"`
	PNG      string `json:"png,omitempty"`
	ID       string `json:"id,omitempty"`
	Username string `json:"username,omitempty"`
	Name     string `json:"name,omitempty"`
	ChatRef  string `json:"chat_ref,omitempty"`

	Percent  float64 `json:"percent,omitempty"`
	Current  int64   `json:"current,omitempty"`
	Total    int64   `json:"total,omitempty"`
	Speed    string  `json:"speed,omitempty"`
	Filename string  `json:"filename,omitempty"`
	// FileID is stable within one download job and identifies one manifest
	// item even when several files report progress concurrently. State is one
	// of queued, downloading, completed, failed, or cancelled.
	FileID     string   `json:"file_id,omitempty"`
	State      string   `json:"state,omitempty"`
	Path       string   `json:"path,omitempty"`
	Count      int64    `json:"count,omitempty"`
	TotalBytes int64    `json:"total_bytes,omitempty"`
	Chats      *[]Chat  `json:"chats,omitempty"`
	Topics     *[]Topic `json:"topics,omitempty"`
	Skipped    *int     `json:"skipped,omitempty"`
	Partial    bool     `json:"partial,omitempty"`
}

// Chat is the machine-readable chat item returned by the desktop chat list.
// Ref is deliberately typed (channel/chat/user) because Telegram can assign
// the same numeric ID to different peer kinds.
type Chat struct {
	ID       string  `json:"id"`
	Ref      string  `json:"ref"`
	Type     string  `json:"type"`
	Title    string  `json:"title"`
	Username string  `json:"username,omitempty"`
	Forum    bool    `json:"forum,omitempty"`
	Topics   []Topic `json:"topics"`
}

type Topic struct {
	ID    int    `json:"id"`
	Title string `json:"title"`
}

type input struct {
	Value   *string `json:"value"`
	Command string  `json:"command"`
}

var secretPattern = regexp.MustCompile(`(?i)(password|passcode|api[_-]?hash|auth[_-]?token|access[_-]?token|session)(\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)`)

// SafeError keeps useful diagnostics while removing values which must never be
// reflected into the desktop event stream.  Authentication failures in the
// Telegram stack normally do not include credentials, but this guard also
// covers wrapped third-party errors and future logging changes.
func SafeError(err error) string {
	if err == nil {
		return ""
	}
	message := strings.TrimSpace(err.Error())
	return secretPattern.ReplaceAllString(message, `$1$2[已隱藏]`)
}

// Enabled reports whether the optional desktop transport was requested.
// TDL_GUI=1 is the documented value; true is accepted as a convenience for
// launchers which expose boolean environment settings.
func Enabled() bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv("TDL_GUI")))
	return v == "1" || v == "true" || v == "yes"
}

// Start creates a Bridge using the process stdio streams when TDL_GUI is
// enabled.  In regular CLI mode it returns the original context and a nil
// bridge, so no goroutine is started and stdin remains untouched.
func Start(parent context.Context) (context.Context, *Bridge) {
	if !Enabled() {
		return parent, nil
	}
	return New(parent, os.Stdin, os.Stdout)
}

// New creates a Bridge around the supplied streams.  It is exported primarily
// for protocol tests and embedding; callers should normally use Start.
func New(parent context.Context, in io.Reader, out io.Writer) (context.Context, *Bridge) {
	ctx, cancel := context.WithCancel(parent)
	if out == nil {
		out = io.Discard
	}
	b := &Bridge{
		out:       out,
		in:        in,
		responses: make(chan string, 8),
		cancel:    cancel,
		done:      make(chan struct{}),
	}

	if in != nil {
		go b.readLoop()
	}
	return With(ctx, b), b
}

// With stores a bridge in ctx.  It is useful when a command creates a child
// context before handing it to an authentication or download helper.
func With(ctx context.Context, b *Bridge) context.Context {
	if b == nil {
		return ctx
	}
	return context.WithValue(ctx, bridgeKey{}, b)
}

// From returns the bridge attached to ctx, or nil in regular CLI mode.
func From(ctx context.Context) *Bridge {
	if ctx == nil {
		return nil
	}
	b, _ := ctx.Value(bridgeKey{}).(*Bridge)
	return b
}

// Emit writes one machine-readable event.  Calls are serialized because
// downloads report progress concurrently with authentication and logging.
func (b *Bridge) Emit(event Event) error {
	if b == nil {
		return nil
	}
	payload, err := json.Marshal(event)
	if err != nil {
		return err
	}

	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	_, err = io.WriteString(b.out, prefix+string(payload)+"\n")
	return err
}

// Emit writes an event through the bridge attached to ctx.  It is a no-op in
// regular CLI mode.
func Emit(ctx context.Context, event Event) error {
	if b := From(ctx); b != nil {
		return b.Emit(event)
	}
	return nil
}

// Prompt emits a prompt and waits for the next JSON input line with a value.
// A cancel command, EOF, or context cancellation unblocks the caller.
func (b *Bridge) Prompt(ctx context.Context, kind, message string) (string, error) {
	if b == nil {
		return "", errors.New("desktop bridge is not enabled")
	}
	if err := b.Emit(Event{Event: "prompt", Kind: kind, Message: message}); err != nil {
		return "", err
	}

	// A finite test reader (and a launcher which closes stdin immediately
	// after sending a value) can make both the response and context ready at
	// once.  Consume an already queued response first.
	select {
	case value := <-b.responses:
		return strings.TrimSpace(value), nil
	default:
	}
	select {
	case value := <-b.responses:
		return strings.TrimSpace(value), nil
	case <-ctx.Done():
		return "", ctx.Err()
	case <-b.done:
		return "", context.Canceled
	}
}

// Prompt uses the bridge in ctx, and returns an error when the application is
// running without a bridge.  Authentication code uses this helper to select
// machine input without duplicating context plumbing.
func Prompt(ctx context.Context, kind, message string) (string, error) {
	b := From(ctx)
	if b == nil {
		return "", errors.New("desktop bridge is not enabled")
	}
	return b.Prompt(ctx, kind, message)
}

// Close stops the bridge's reader and releases its child context.  The reader
// may still be blocked in an OS pipe until process exit; done ensures pending
// prompts return immediately.
func (b *Bridge) Close() {
	if b == nil {
		return
	}
	b.closeMu.Do(func() {
		close(b.done)
		b.cancel()
	})
}

func (b *Bridge) readLoop() {
	scanner := bufio.NewScanner(b.in)
	// JSON requests are intentionally small, but allowing a larger line avoids
	// surprising failures when a launcher includes a long pasted value.
	scanner.Buffer(make([]byte, 1024), 1024*1024)
	for scanner.Scan() {
		var req input
		if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
			continue
		}
		if req.Command == "cancel" {
			b.cancel()
			continue
		}
		if req.Value == nil {
			continue
		}
		select {
		case b.responses <- *req.Value:
		case <-b.done:
			return
		}
	}
	// EOF means that a launcher disappeared.  Do not leave authentication or
	// downloads waiting forever for an answer which can no longer arrive.
	b.cancel()
}

type bridgeKey struct{}
