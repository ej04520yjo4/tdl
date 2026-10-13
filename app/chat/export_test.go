package chat

import (
	"math"
	"testing"

	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
)

func TestParseMediaTypes(t *testing.T) {
	got, err := ParseMediaTypes(" video,PHOTO,video ")
	if err != nil {
		t.Fatalf("ParseMediaTypes returned error: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("types = %#v, want two unique types", got)
	}
	if _, ok := got[MediaTypeVideo]; !ok {
		t.Fatal("video type missing")
	}
	if _, ok := got[MediaTypePhoto]; !ok {
		t.Fatal("photo type missing")
	}

	if got, err := ParseMediaTypes(""); err != nil || len(got) != 0 {
		t.Fatalf("empty media filter = %#v, %v; want empty filter", got, err)
	}
	if _, err := ParseMediaTypes("archive"); err == nil {
		t.Fatal("invalid media type accepted")
	}
}

func TestMessageMediaType(t *testing.T) {
	tests := []struct {
		name string
		msg  *tg.Message
		want string
	}{
		{
			name: "photo",
			msg: func() *tg.Message {
				m := &tg.Message{}
				m.SetMedia(&tg.MessageMediaPhoto{Photo: &tg.Photo{}})
				return m
			}(),
			want: MediaTypePhoto,
		},
		{
			name: "video document",
			msg: func() *tg.Message {
				m := &tg.Message{}
				m.SetMedia(&tg.MessageMediaDocument{
					Document: &tg.Document{Attributes: []tg.DocumentAttributeClass{&tg.DocumentAttributeVideo{}}},
				})
				return m
			}(),
			want: MediaTypeVideo,
		},
		{
			name: "audio document",
			msg: func() *tg.Message {
				m := &tg.Message{}
				m.SetMedia(&tg.MessageMediaDocument{
					Document: &tg.Document{Attributes: []tg.DocumentAttributeClass{&tg.DocumentAttributeAudio{}}},
				})
				return m
			}(),
			want: MediaTypeAudio,
		},
		{
			name: "video mime fallback",
			msg: func() *tg.Message {
				m := &tg.Message{}
				m.SetMedia(&tg.MessageMediaDocument{Document: &tg.Document{MimeType: "video/mp4"}})
				return m
			}(),
			want: MediaTypeVideo,
		},
		{
			name: "audio mime fallback",
			msg: func() *tg.Message {
				m := &tg.Message{}
				m.SetMedia(&tg.MessageMediaDocument{Document: &tg.Document{MimeType: "audio/mpeg"}})
				return m
			}(),
			want: MediaTypeAudio,
		},
		{
			name: "generic document",
			msg: func() *tg.Message {
				m := &tg.Message{}
				m.SetMedia(&tg.MessageMediaDocument{Document: &tg.Document{}})
				return m
			}(),
			want: MediaTypeDocument,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, ok := messageMediaType(tt.msg)
			if !ok || got != tt.want {
				t.Fatalf("messageMediaType = %q, %v; want %q, true", got, ok, tt.want)
			}
		})
	}

	if got, ok := messageMediaType(&tg.Message{}); ok || got != "" {
		t.Fatalf("empty message media type = %q, %v", got, ok)
	}
}

func TestTypedPeerRefKeepsPeerKindsDistinct(t *testing.T) {
	if got := typedPeerRef(&tg.PeerChat{ChatID: 123}); got != "chat:123" {
		t.Fatalf("chat ref = %q", got)
	}
	if got := typedPeerRef(&tg.PeerChannel{ChannelID: 123}); got != "channel:123" {
		t.Fatalf("channel ref = %q", got)
	}
	if got := typedPeerRef(&tg.PeerUser{UserID: 123}); got != "user:123" {
		t.Fatalf("user ref = %q", got)
	}
}

func TestSelectThreadDistinguishesForumTopicAndReply(t *testing.T) {
	tests := []struct {
		name string
		opts ExportOptions
		id   int
		kind string
	}{
		{name: "forum general", opts: ExportOptions{Topic: 1}, id: 1, kind: "topic"},
		{name: "forum topic", opts: ExportOptions{Topic: 42}, id: 42, kind: "topic"},
		{name: "channel reply", opts: ExportOptions{Reply: 77}, id: 77, kind: "reply"},
		{name: "legacy thread", opts: ExportOptions{Thread: 88}, id: 88, kind: "legacy"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := selectThread(tt.opts)
			if err != nil {
				t.Fatalf("selectThread returned error: %v", err)
			}
			if got.id != tt.id || got.kind != tt.kind {
				t.Fatalf("selection = %#v, want id=%d kind=%q", got, tt.id, tt.kind)
			}
		})
	}

	if _, err := selectThread(ExportOptions{Topic: 1, Reply: 2}); err == nil {
		t.Fatal("topic and reply conflict was accepted")
	}
	for _, topicID := range []int{-1, math.MaxInt32 + 1} {
		if _, err := selectThread(ExportOptions{Topic: topicID}); err == nil {
			t.Fatalf("invalid topic id %d was accepted", topicID)
		}
	}
	if got, err := selectThread(ExportOptions{Topic: math.MaxInt32}); err != nil || got.id != math.MaxInt32 {
		t.Fatalf("max topic id = %#v, %v", got, err)
	}
}

func TestValidateForumTopicPeerRequiresForumChannel(t *testing.T) {
	manager := (&peers.Options{}).Build(nil)
	forum := manager.Channel(&tg.Channel{ID: 1, Title: "forum", Forum: true})
	nonForum := manager.Channel(&tg.Channel{ID: 2, Title: "channel"})
	group := manager.Chat(&tg.Chat{ID: 3, Title: "group"})

	if err := validateForumTopicPeer(forum); err != nil {
		t.Fatalf("forum channel rejected: %v", err)
	}
	for name, peer := range map[string]peers.Peer{
		"non-forum channel": nonForum,
		"basic group":       group,
		"nil peer":          nil,
	} {
		t.Run(name, func(t *testing.T) {
			if err := validateForumTopicPeer(peer); err == nil {
				t.Fatalf("peer accepted for forum topic export: %T", peer)
			}
		})
	}
}

func TestMessageIsGeneralTopic(t *testing.T) {
	general := &tg.Message{}
	if !messageIsGeneralTopic(general) {
		t.Fatal("message without reply header should belong to General")
	}

	replyInGeneral := &tg.Message{}
	generalHeader := &tg.MessageReplyHeader{}
	generalHeader.SetReplyToMsgID(12)
	replyInGeneral.SetReplyTo(generalHeader)
	if !messageIsGeneralTopic(replyInGeneral) {
		t.Fatal("reply without top_msg_id should belong to General")
	}

	otherTopic := &tg.Message{}
	topicHeader := &tg.MessageReplyHeader{}
	topicHeader.SetForumTopic(true)
	topicHeader.SetReplyToTopID(42)
	otherTopic.SetReplyTo(topicHeader)
	if messageIsGeneralTopic(otherTopic) {
		t.Fatal("message with top_msg_id=42 should not belong to General")
	}

	generalTopID := &tg.Message{}
	topHeader := &tg.MessageReplyHeader{}
	topHeader.SetReplyToTopID(1)
	generalTopID.SetReplyTo(topHeader)
	if !messageIsGeneralTopic(generalTopID) {
		t.Fatal("top_msg_id=1 should be accepted as General")
	}

	ambiguous := &tg.Message{}
	ambiguousHeader := &tg.MessageReplyHeader{}
	ambiguousHeader.SetForumTopic(true)
	ambiguous.SetReplyTo(ambiguousHeader)
	if messageIsGeneralTopic(ambiguous) {
		t.Fatal("forum header without top_msg_id should not be assumed General")
	}
}

func TestTelegramOffsetSkipsUnrepresentableUpperBound(t *testing.T) {
	max32 := int(math.MaxInt32)
	if got, ok := telegramOffset(max32 - 1); !ok || got != max32 {
		t.Fatalf("telegramOffset(%d) = %d, %v; want %d, true", max32-1, got, ok, max32)
	}

	maxInt := int(^uint(0) >> 1)
	for _, upper := range []int{max32, maxInt} {
		if got, ok := telegramOffset(upper); ok {
			t.Fatalf("telegramOffset(%d) = %d, true; want no offset", upper, got)
		}
	}
}
