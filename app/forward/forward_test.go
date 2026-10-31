package forward

import (
	"context"
	"strings"
	"testing"

	"github.com/iyear/tdl/pkg/tmessage"
)

func TestOptionsValidateTopicContract(t *testing.T) {
	const maxTopicID = int64(1<<31 - 1)

	tests := []struct {
		name    string
		opts    Options
		wantErr string
	}{
		{name: "legacy zero without destination", opts: Options{}},
		{name: "legacy zero expression", opts: Options{To: `{"peer":"channel:1"}`}},
		{name: "typed channel", opts: Options{To: "channel:123", TopicID: 42}},
		{name: "typed group", opts: Options{To: "chat:123", TopicID: 42}},
		{name: "typed group alias", opts: Options{To: "group:123", TopicID: 42}},
		{name: "maximum wire topic", opts: Options{To: "channel:123", TopicID: int(maxTopicID)}},
		{
			name:    "topic requires destination",
			opts:    Options{TopicID: 1},
			wantErr: "--to should be set",
		},
		{
			name:    "negative topic",
			opts:    Options{To: "channel:123", TopicID: -1},
			wantErr: "invalid --topic",
		},
		{
			name:    "topic exceeds wire integer",
			opts:    Options{To: "channel:123", TopicID: int(maxTopicID + 1)},
			wantErr: "invalid --topic",
		},
		{
			name:    "user is not a forum destination",
			opts:    Options{To: "user:123", TopicID: 1},
			wantErr: "typed destination",
		},
		{
			name:    "plain peer is not explicit enough",
			opts:    Options{To: "123", TopicID: 1},
			wantErr: "typed destination",
		},
		{
			name:    "expression is not a constant destination",
			opts:    Options{To: `{"peer":"channel:123"}`, TopicID: 1},
			wantErr: "typed destination",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := tt.opts.Validate()
			if tt.wantErr == "" {
				if err != nil {
					t.Fatalf("Validate() error = %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("Validate() error = %v, want substring %q", err, tt.wantErr)
			}
		})
	}
}

func TestIterSkipsEmptyDialogs(t *testing.T) {
	i := newIter(iterOptions{
		dialogs: []*tmessage.Dialog{
			nil,
			{Messages: nil},
			{Messages: []int{}},
		},
	})

	if i.Next(context.Background()) {
		t.Fatal("empty dialogs produced an element")
	}
	if err := i.Err(); err != nil {
		t.Fatalf("empty dialogs set iterator error: %v", err)
	}
}

func TestTotalMessagesEmptyManifest(t *testing.T) {
	if got := totalMessages([]*tmessage.Dialog{nil, {Messages: nil}, {Messages: []int{}}}); got != 0 {
		t.Fatalf("totalMessages(empty) = %d, want 0", got)
	}
}

func TestSingleOptionDisablesAlbumExpansion(t *testing.T) {
	// Run maps --single to iterOptions.grouped=false. Keep this assertion close
	// to the forward iterator because expanding an album would fetch messages
	// outside a date/media/topic manifest.
	single := &iterElem{opts: iterOptions{grouped: false}}
	if single.AsGrouped() {
		t.Fatal("single forward unexpectedly enables grouped expansion")
	}
	grouped := &iterElem{opts: iterOptions{grouped: true}}
	if !grouped.AsGrouped() {
		t.Fatal("grouped forward unexpectedly disables grouped expansion")
	}
}
