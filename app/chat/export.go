package chat

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/expr-lang/expr"
	"github.com/fatih/color"
	"github.com/go-faster/jx"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/telegram/query"
	"github.com/gotd/td/telegram/query/messages"
	"github.com/gotd/td/tg"
	"github.com/jedib0t/go-pretty/v6/progress"
	"go.uber.org/multierr"

	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/core/tmedia"
	"github.com/iyear/tdl/core/util/tutil"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/prog"
	"github.com/iyear/tdl/pkg/texpr"
)

//go:generate go-enum --names --values --flag --nocase

type ExportOptions struct {
	Type ExportType
	Chat string
	// Topic selects a forum topic. Topic 1 is Telegram's General topic and
	// must be read from history because it has no top_msg_id.
	Topic int
	// Reply selects the root message of a channel discussion thread.
	Reply int
	// Thread is kept as a compatibility alias for callers which used the old
	// --topic/--reply field. New callers should use Topic or Reply so General
	// topic semantics can be applied unambiguously.
	Thread      int
	Input       []int
	Output      string
	Filter      string
	OnlyMedia   bool
	MediaTypes  string
	WithContent bool
	Raw         bool
	All         bool
}

type threadSelection struct {
	id    int
	kind  string
	forum bool
}

func selectThread(opts ExportOptions) (threadSelection, error) {
	if opts.Topic != 0 && opts.Reply != 0 {
		return threadSelection{}, fmt.Errorf("--topic and --reply cannot be used together")
	}
	if opts.Topic != 0 {
		if opts.Topic < 1 || int64(opts.Topic) > math.MaxInt32 {
			return threadSelection{}, fmt.Errorf("forum topic id must be between 1 and %d", math.MaxInt32)
		}
		return threadSelection{id: opts.Topic, kind: "topic", forum: true}, nil
	}
	if opts.Reply != 0 {
		return threadSelection{id: opts.Reply, kind: "reply"}, nil
	}
	if opts.Thread != 0 {
		// The legacy field did not distinguish a forum topic from a channel
		// reply. Preserve its historical getReplies behavior.
		return threadSelection{id: opts.Thread, kind: "legacy"}, nil
	}
	return threadSelection{}, nil
}

type Message struct {
	ID        int         `json:"id"`
	Type      string      `json:"type"`
	File      string      `json:"file"`
	Date      int         `json:"date,omitempty"`
	Text      string      `json:"text,omitempty"`
	MediaType string      `json:"media_type,omitempty"`
	Size      int64       `json:"size,omitempty"`
	Raw       *tg.Message `json:"raw,omitempty"`
}

const (
	MediaTypeVideo    = "video"
	MediaTypePhoto    = "photo"
	MediaTypeAudio    = "audio"
	MediaTypeDocument = "document"
)

var validMediaTypes = map[string]struct{}{
	MediaTypeVideo: {}, MediaTypePhoto: {}, MediaTypeAudio: {}, MediaTypeDocument: {},
}

// ParseMediaTypes validates and normalizes the comma-separated filter used by
// the desktop client. An empty value intentionally preserves the historical
// export behavior.
func ParseMediaTypes(raw string) (map[string]struct{}, error) {
	result := make(map[string]struct{})
	if strings.TrimSpace(raw) == "" {
		return result, nil
	}
	for _, value := range strings.Split(raw, ",") {
		value = strings.ToLower(strings.TrimSpace(value))
		if value == "" {
			continue
		}
		if _, ok := validMediaTypes[value]; !ok {
			return nil, fmt.Errorf("unknown media type %q (choose video, photo, audio, document)", value)
		}
		result[value] = struct{}{}
	}
	if len(result) == 0 {
		return nil, fmt.Errorf("media-types must contain at least one type")
	}
	return result, nil
}

func resolvePeer(ctx context.Context, manager *peers.Manager, value string) (peers.Peer, error) {
	value = strings.TrimSpace(value)
	kind, rawID, ok := strings.Cut(value, ":")
	if !ok {
		return tutil.GetInputPeer(ctx, manager, value)
	}
	id, err := strconv.ParseInt(strings.TrimSpace(rawID), 10, 64)
	if err != nil || id <= 0 {
		return nil, fmt.Errorf("invalid typed chat reference %q", value)
	}
	switch strings.ToLower(strings.TrimSpace(kind)) {
	case "channel":
		return manager.ResolveChannelID(ctx, id)
	case "chat", "group":
		return manager.ResolveChatID(ctx, id)
	case "user", "private":
		return manager.ResolveUserID(ctx, id)
	default:
		return nil, fmt.Errorf("unknown typed chat reference %q", value)
	}
}

func peerType(peer peers.Peer) string {
	switch peer.(type) {
	case peers.Channel:
		return "channel"
	case peers.Chat:
		return "chat"
	case peers.User:
		return "user"
	default:
		return ""
	}
}

func messageMediaType(m *tg.Message) (string, bool) {
	media, ok := m.GetMedia()
	if !ok {
		return "", false
	}
	switch media := media.(type) {
	case *tg.MessageMediaPhoto:
		return MediaTypePhoto, true
	case *tg.MessageMediaDocument:
		doc, ok := media.Document.(*tg.Document)
		if !ok {
			return "", false
		}
		for _, attr := range doc.Attributes {
			switch attr.(type) {
			case *tg.DocumentAttributeVideo:
				return MediaTypeVideo, true
			case *tg.DocumentAttributeAudio:
				return MediaTypeAudio, true
			}
		}
		// Telegram clients may omit the media attribute when a video or
		// audio file is sent as a generic document. MIME is authoritative
		// enough for the desktop media filter in that case.
		if strings.HasPrefix(strings.ToLower(doc.MimeType), "video/") {
			return MediaTypeVideo, true
		}
		if strings.HasPrefix(strings.ToLower(doc.MimeType), "audio/") {
			return MediaTypeAudio, true
		}
		return MediaTypeDocument, true
	default:
		return "", false
	}
}

// messageIsGeneralTopic reports whether a message belongs to Telegram's
// General forum topic. General messages do not carry top_msg_id. A few API
// responses explicitly carry top_msg_id=1, so accept that representation as
// well. A ForumTopic header without a top id is treated as non-General: it is
// safer to omit an ambiguous message than to download a message from another
// topic when the user selected General.
func messageIsGeneralTopic(m *tg.Message) bool {
	header, ok := m.GetReplyTo()
	if !ok || header == nil {
		return true
	}
	forumHeader, ok := header.(*tg.MessageReplyHeader)
	if !ok || forumHeader == nil {
		return false
	}
	if topID, hasTopID := forumHeader.GetReplyToTopID(); hasTopID && topID != 0 {
		return topID == 1
	}
	if forumHeader.GetForumTopic() {
		return false
	}
	return true
}

// ExportType
// ENUM(time, id, last)
type ExportType int

func Export(ctx context.Context, c *telegram.Client, kvd storage.Storage, opts ExportOptions) (rerr error) {
	gui := desktopio.From(ctx) != nil
	mediaTypes, err := ParseMediaTypes(opts.MediaTypes)
	if err != nil {
		return err
	}
	thread, err := selectThread(opts)
	if err != nil {
		return err
	}

	// only output available fields
	if opts.Filter == "-" {
		fg := texpr.NewFieldsGetter(nil)

		fields, err := fg.Walk(&texpr.EnvMessage{})
		if err != nil {
			return fmt.Errorf("failed to walk fields: %w", err)
		}

		fmt.Print(fg.Sprint(fields, true))
		return nil
	}

	filter, err := expr.Compile(opts.Filter, expr.AsBool())
	if err != nil {
		return fmt.Errorf("failed to compile filter: %w", err)
	}

	var peer peers.Peer

	manager := peers.Options{Storage: storage.NewPeers(kvd)}.Build(c.API())
	if opts.Chat == "" { // defaults to me(saved messages)
		peer, err = manager.Self(ctx)
	} else {
		peer, err = resolvePeer(ctx, manager, opts.Chat)
	}
	if err != nil {
		return fmt.Errorf("failed to get peer: %w", err)
	}
	if thread.forum {
		if err := validateForumTopicPeer(peer); err != nil {
			return err
		}
	}

	if !gui {
		color.Yellow("WARN: Export only generates minimal JSON for tdl download, not for backup.")
		color.Cyan("Occasional suspensions are due to Telegram rate limitations, please wait a moment.")
		fmt.Println()

		color.Blue("Type: %s | Input: %v", opts.Type, opts.Input)
	} else {
		_ = desktopio.Emit(ctx, desktopio.Event{
			Event:   "status",
			Kind:    "chat_export",
			Stage:   "scanning",
			Message: "正在掃描訊息與媒體…",
		})
	}

	pw := prog.New(progress.FormatNumber)
	pw.SetUpdateFrequency(200 * time.Millisecond)
	pw.Style().Visibility.TrackerOverall = false
	pw.Style().Visibility.ETA = false
	pw.Style().Visibility.Percentage = false

	tracker := prog.AppendTracker(pw, progress.FormatNumber, fmt.Sprintf("%s-%d", peer.VisibleName(), peer.ID()), 0)

	if !gui {
		go pw.Render()
	}

	var q messages.Query
	switch {
	case thread.kind == "topic" && thread.id == 1:
		// The General forum topic has no top_msg_id. getReplies(peer, 1)
		// therefore does not mean "General"; scan history and filter by the
		// reply header below.
		q = query.NewQuery(c.API()).Messages().GetHistory(peer.InputPeer())
	case thread.id != 0: // non-General forum topics and channel replies
		q = query.NewQuery(c.API()).Messages().GetReplies(peer.InputPeer()).MsgID(thread.id)
	default: // history
		q = query.NewQuery(c.API()).Messages().GetHistory(peer.InputPeer())
	}
	iter := messages.NewIterator(q, 100)

	switch opts.Type {
	case ExportTypeTime:
		if offset, ok := telegramOffset(opts.Input[1]); ok {
			iter = iter.OffsetDate(offset)
		}
	case ExportTypeId:
		if offset, ok := telegramOffset(opts.Input[1]); ok {
			iter = iter.OffsetID(offset) // #89: retain the last msg id
		}
	case ExportTypeLast:
	}

	outputPath, err := filepath.Abs(opts.Output)
	if err != nil {
		return fmt.Errorf("resolve output path: %w", err)
	}
	f, err := os.Create(outputPath)
	if err != nil {
		return err
	}
	defer multierr.AppendInvoke(&rerr, multierr.Close(f))

	enc := jx.NewStreamingEncoder(f, 512)
	finalized := false
	defer func() {
		if finalized {
			return
		}
		// Keep the file structurally valid for callers that inspect a failed
		// export, while the error returned by Export remains authoritative.
		_ = enc.ArrEnd()
		_ = enc.ObjEnd()
		_ = enc.Close()
	}()

	// process thread is reply type and peer is broadcast channel,
	// so we need to set discussion group id instead of broadcast id
	id := peer.ID()
	if p, ok := peer.(peers.Channel); thread.id != 0 && ok && p.IsBroadcast() {
		bc, _ := p.ToBroadcast()
		raw, err := bc.FullRaw(ctx)
		if err != nil {
			return fmt.Errorf("failed to get broadcast full raw: %w", err)
		}

		if id, ok = raw.GetLinkedChatID(); !ok {
			return fmt.Errorf("no linked group")
		}
	}

	enc.ObjStart()
	enc.Field("id", func(e *jx.Encoder) { e.Int64(id) })
	enc.Field("peer_type", func(e *jx.Encoder) { e.Str(peerType(peer)) })

	enc.FieldStart("messages")
	enc.ArrStart()

	count := int64(0)
	var totalBytes int64

loop:
	for iter.Next(ctx) {
		msg := iter.Value()
		messageDate := msg.Msg.GetDate()
		switch opts.Type {
		case ExportTypeTime:
			if messageDate < opts.Input[0] {
				break loop
			}
			if messageDate > opts.Input[1] {
				continue
			}
		case ExportTypeId:
			if msg.Msg.GetID() < opts.Input[0] {
				break loop
			}
		case ExportTypeLast:
			if count >= int64(opts.Input[0]) {
				break loop
			}
		}

		m, ok := msg.Msg.(*tg.Message)
		if !ok {
			continue
		}
		if thread.kind == "topic" && thread.id == 1 && !messageIsGeneralTopic(m) {
			continue
		}
		// only get media messages
		media, hasMedia := tmedia.GetMedia(m)
		kind, typedMedia := messageMediaType(m)
		if len(mediaTypes) > 0 {
			// A typed media kind without a downloadable tmedia.Media (for
			// example DocumentEmpty) must not enter a download manifest.
			if !typedMedia || !hasMedia {
				continue
			}
			if _, ok := mediaTypes[kind]; !ok {
				continue
			}
		} else if !hasMedia && !opts.All {
			continue
		}

		b, err := texpr.Run(filter, texpr.ConvertEnvMessage(m))
		if err != nil {
			return fmt.Errorf("failed to run filter: %w", err)
		}
		if !b.(bool) { // filtered
			continue
		}

		fileName := ""
		if media != nil { // #207
			fileName = media.Name
		}
		t := &Message{
			ID:        m.ID,
			Type:      "message",
			File:      fileName,
			MediaType: kind,
			Size:      mediaSize(media),
		}
		if opts.WithContent || gui {
			t.Date = messageDate
		}
		if opts.WithContent {
			t.Text = m.Message
		}
		if opts.Raw {
			t.Raw = m
		}

		mb, err := json.Marshal(t)
		if err != nil {
			return fmt.Errorf("failed to marshal message: %w", err)
		}
		enc.Raw(mb)

		count++
		if media != nil {
			totalBytes += media.Size
		}
		tracker.SetValue(count)
		if gui {
			_ = desktopio.Emit(ctx, desktopio.Event{
				Event:   "status",
				Kind:    "chat_export",
				Stage:   "scanning",
				Current: count,
				Message: fmt.Sprintf("已找到 %d 筆媒體", count),
			})
		}
	}

	if err = iter.Err(); err != nil {
		return err
	}

	enc.ArrEnd()
	enc.ObjEnd()
	if err = enc.Close(); err != nil {
		return err
	}
	finalized = true

	tracker.MarkAsDone()
	if !gui {
		prog.Wait(ctx, pw)
	} else {
		_ = desktopio.Emit(ctx, desktopio.Event{
			Event:      "exported",
			Path:       outputPath,
			Count:      count,
			TotalBytes: totalBytes,
		})
	}
	return nil
}

func validateForumTopicPeer(peer peers.Peer) error {
	channel, ok := peer.(peers.Channel)
	if !ok || channel.Raw() == nil || !channel.Raw().Forum {
		return fmt.Errorf("forum topic export requires a forum channel")
	}
	return nil
}

func mediaSize(media *tmedia.Media) int64 {
	if media == nil {
		return 0
	}
	return media.Size
}

// telegramOffset returns the exclusive upper-bound offset accepted by
// Telegram's 32-bit date/message-id fields.  An open-ended upper bound must
// leave the iterator at its default (latest messages) because adding one to
// MaxInt32, MaxInt64, or another oversized int would wrap before reaching the
// wire.  Export's loop still enforces both bounds after iteration.
func telegramOffset(upper int) (int, bool) {
	if int64(upper) >= math.MaxInt32 {
		return 0, false
	}
	return upper + 1, true
}
