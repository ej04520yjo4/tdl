package desktopio

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestChatsEventProtocol(t *testing.T) {
	var out bytes.Buffer
	ctx, bridge := New(context.Background(), nil, &out)
	defer bridge.Close()

	items := []Chat{{
		ID:    "123",
		Ref:   "channel:123",
		Type:  "group",
		Title: "Forum",
		Topics: []Topic{{
			ID:    7,
			Title: "General",
		}},
	}}
	skipped := 0
	if err := Emit(ctx, Event{Event: "chats", Chats: &items, Skipped: &skipped}); err != nil {
		t.Fatal(err)
	}
	line := strings.TrimPrefix(strings.TrimSpace(out.String()), Prefix)
	var got struct {
		Event   string `json:"event"`
		Chats   []Chat `json:"chats"`
		Skipped int    `json:"skipped"`
	}
	if err := json.Unmarshal([]byte(line), &got); err != nil {
		t.Fatal(err)
	}
	if got.Event != "chats" || len(got.Chats) != 1 || got.Chats[0].Ref != "channel:123" || got.Skipped != 0 {
		t.Fatalf("event = %#v", got)
	}
}

func TestTopicsEventProtocol(t *testing.T) {
	var out bytes.Buffer
	ctx, bridge := New(context.Background(), nil, &out)
	defer bridge.Close()

	items := []Topic{{ID: 1, Title: "General"}, {ID: 9, Title: "公告"}}
	if err := Emit(ctx, Event{Event: "topics", ChatRef: "channel:123", Topics: &items}); err != nil {
		t.Fatal(err)
	}
	line := strings.TrimPrefix(strings.TrimSpace(out.String()), Prefix)
	var got struct {
		Event   string  `json:"event"`
		ChatRef string  `json:"chat_ref"`
		Topics  []Topic `json:"topics"`
	}
	if err := json.Unmarshal([]byte(line), &got); err != nil {
		t.Fatal(err)
	}
	if got.Event != "topics" || got.ChatRef != "channel:123" || len(got.Topics) != 2 || got.Topics[0].ID != 1 {
		t.Fatalf("event = %#v", got)
	}
}

func TestPromptProtocol(t *testing.T) {
	var out bytes.Buffer
	ctx, bridge := New(context.Background(), strings.NewReader(`{"value":"+886912345678"}`+"\n"), &out)
	defer bridge.Close()

	value, err := bridge.Prompt(ctx, "phone", "請輸入電話")
	if err != nil {
		t.Fatalf("prompt failed: %v", err)
	}
	if value != "+886912345678" {
		t.Fatalf("value = %q", value)
	}
	got := out.String()
	if !strings.HasPrefix(got, prefix+`{"event":"prompt"`) {
		t.Fatalf("unexpected event: %q", got)
	}
	if !strings.Contains(got, `"kind":"phone"`) || !strings.Contains(got, `"message":"請輸入電話"`) {
		t.Fatalf("prompt fields missing: %q", got)
	}
}

func TestCancelInputCancelsContext(t *testing.T) {
	ctx, bridge := New(context.Background(), strings.NewReader(`{"command":"cancel"}`+"\n"), &bytes.Buffer{})
	defer bridge.Close()

	select {
	case <-ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("cancel command did not cancel context")
	}

	if _, err := bridge.Prompt(ctx, "code", "不應等待"); err == nil {
		t.Fatal("prompt succeeded after cancellation")
	}
}
