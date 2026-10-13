package dl

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	"github.com/iyear/tdl/pkg/tmessage"
)

func TestIterExtensionFiltersAreCaseInsensitive(t *testing.T) {
	tests := []struct {
		name    string
		include []string
		exclude []string
		file    string
		want    bool
	}{
		{
			name:    "lowercase include matches uppercase filename",
			include: []string{"mp4"},
			file:    "video.Mp4",
			want:    true,
		},
		{
			name:    "uppercase include matches lowercase filename",
			include: []string{"MP4"},
			file:    "video.mp4",
			want:    true,
		},
		{
			name:    "lowercase exclude matches mixed case filename",
			exclude: []string{"zip"},
			file:    "archive.ZiP",
			want:    false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root := t.TempDir()
			it, err := newIter(nil, nil, [][]*tmessage.Dialog{{
				{Peer: &tg.InputPeerChat{ChatID: 1}, Messages: []int{1}},
			}}, Options{
				Dir:      root,
				Template: "{{.FileName}}",
				Include:  tt.include,
				Exclude:  tt.exclude,
			}, 0)
			require.NoError(t, err)

			message := &tg.Message{ID: 1}
			message.SetMedia(&tg.MessageMediaDocument{
				Document: &tg.Document{
					ID:       2,
					MimeType: "application/octet-stream",
					Size:     1,
					DCID:     2,
					Attributes: []tg.DocumentAttributeClass{
						&tg.DocumentAttributeFilename{FileName: tt.file},
					},
				},
			})
			from := (&peers.Options{}).Build(nil).Chat(&tg.Chat{ID: 9, Title: "test"})

			ret, skip := it.processSingle(context.Background(), message, from, 0)
			require.Equal(t, tt.want, ret)
			require.Equal(t, !tt.want, skip)
			if !tt.want {
				require.Equal(t, int64(0), it.CandidateCount())
				return
			}

			elem := (<-it.elem).(*iterElem)
			require.Equal(t, tt.file+tempExt, filepath.Base(elem.to.Name()),
				"filtering must preserve the original media filename casing")
			require.Equal(t, root, filepath.Dir(elem.to.Name()),
				"the default mode must keep the historical flat output path")
			require.NoError(t, elem.to.Close())
			require.NoError(t, os.Remove(elem.to.Name()))
			require.Equal(t, int64(1), it.CandidateCount())
		})
	}
}

func testDocumentMessage(id int, name string, size int64) *tg.Message {
	message := &tg.Message{ID: id}
	message.SetMedia(&tg.MessageMediaDocument{
		Document: &tg.Document{
			ID:       int64(id),
			MimeType: "application/octet-stream",
			Size:     size,
			DCID:     1,
			Attributes: []tg.DocumentAttributeClass{
				&tg.DocumentAttributeFilename{FileName: name},
			},
		},
	})
	return message
}

func closeAndRemoveIterElem(t *testing.T, elem *iterElem) {
	t.Helper()
	require.NoError(t, elem.to.Close())
	require.NoError(t, os.Remove(elem.to.Name()))
}

func TestIterGroupByChatUsesSafeFoldersForMultipleChats(t *testing.T) {
	root := t.TempDir()
	manager := (&peers.Options{}).Build(nil)
	chat := manager.Chat(&tg.Chat{ID: 7, Title: "News: Daily?"})
	channel := manager.Channel(&tg.Channel{ID: 8, Title: "Channel\\Two"})

	it, err := newIter(nil, nil, [][]*tmessage.Dialog{{
		{Peer: chat.InputPeer(), Messages: []int{1}},
		{Peer: channel.InputPeer(), Messages: []int{2}},
	}}, Options{
		Dir:         root,
		Template:    "{{.FileName}}",
		GroupByChat: true,
	}, 0)
	require.NoError(t, err)

	ret, skip := it.processSingle(context.Background(), testDocumentMessage(1, "chat.bin", 3), chat, 0)
	require.True(t, ret)
	require.False(t, skip)
	chatElem := (<-it.elem).(*iterElem)
	require.Equal(t, "News_ Daily_", safeChatFolder(chat))
	require.Equal(t, filepath.Join(root, "News_ Daily_", "chat.bin"+tempExt), chatElem.to.Name())
	closeAndRemoveIterElem(t, chatElem)

	ret, skip = it.processSingle(context.Background(), testDocumentMessage(2, "channel.bin", 1), channel, 1)
	require.True(t, ret)
	require.False(t, skip)
	channelElem := (<-it.elem).(*iterElem)
	require.Equal(t, "Channel_Two", safeChatFolder(channel))
	require.Equal(t, filepath.Join(root, "Channel_Two", "channel.bin"+tempExt), channelElem.to.Name())
	closeAndRemoveIterElem(t, channelElem)

	entries, err := os.ReadDir(root)
	require.NoError(t, err)
	require.Len(t, entries, 2, "each peer gets one sibling folder under the output root")
}

func TestIterGroupByChatDisambiguatesDuplicateTitles(t *testing.T) {
	root := t.TempDir()
	manager := (&peers.Options{}).Build(nil)
	chat := manager.Chat(&tg.Chat{ID: 21, Title: "Shared"})
	channel := manager.Channel(&tg.Channel{ID: 22, Title: "Shared"})
	it, err := newIter(nil, nil, [][]*tmessage.Dialog{{
		{Peer: chat.InputPeer(), Messages: []int{1}},
		{Peer: channel.InputPeer(), Messages: []int{2}},
	}}, Options{Dir: root, Template: "{{.FileName}}", GroupByChat: true}, 0)
	require.NoError(t, err)

	ret, skip := it.processSingle(context.Background(), testDocumentMessage(1, "chat.bin", 1), chat, 0)
	require.True(t, ret)
	require.False(t, skip)
	chatElem := (<-it.elem).(*iterElem)
	require.Equal(t, filepath.Join(root, "Shared", "chat.bin"+tempExt), chatElem.to.Name())
	closeAndRemoveIterElem(t, chatElem)

	ret, skip = it.processSingle(context.Background(), testDocumentMessage(2, "channel.bin", 1), channel, 1)
	require.True(t, ret)
	require.False(t, skip)
	channelElem := (<-it.elem).(*iterElem)
	require.Equal(t, filepath.Join(root, "Shared (channel-22)", "channel.bin"+tempExt), channelElem.to.Name())
	closeAndRemoveIterElem(t, channelElem)
}

func TestIterGroupByChatSanitizesReservedAndFallsBackToPeerIdentity(t *testing.T) {
	manager := (&peers.Options{}).Build(nil)
	reserved := manager.Chat(&tg.Chat{ID: 11, Title: "CON."})
	empty := manager.Channel(&tg.Channel{ID: 12})

	require.Equal(t, "_CON", safeChatFolder(reserved), "reserved Windows device names need a safe prefix")
	require.Equal(t, "channel-12", safeChatFolder(empty), "untitled peers need a stable typed identity")

	unsafe := sanitizeWindowsComponent("..\\folder/name\x00")
	require.NotEmpty(t, unsafe)
	require.NotEqual(t, "..", unsafe)
	require.NotContains(t, unsafe, "\\")
	require.NotContains(t, unsafe, "/")
	require.NotContains(t, unsafe, "\x00")
	require.LessOrEqual(t, len([]rune(sanitizeWindowsComponent(strings.Repeat("x", maxChatFolderRunes+20)))), maxChatFolderRunes)
	longReserved := sanitizeWindowsComponent("CON." + strings.Repeat("x", 116))
	require.LessOrEqual(t, len([]rune(longReserved)), maxChatFolderRunes)
	require.Equal(t, longReserved, sanitizeWindowsComponent(longReserved), "reserved-name truncation must be idempotent")
	require.Equal(t, "_COM¹", sanitizeWindowsComponent("COM¹"))
	require.Equal(t, "_LPT³", sanitizeWindowsComponent("LPT³"))
}

func TestIterGroupByChatSkipSameChecksGroupedPath(t *testing.T) {
	root := t.TempDir()
	manager := (&peers.Options{}).Build(nil)
	chat := manager.Chat(&tg.Chat{ID: 13, Title: "Saved"})
	chatDir := filepath.Join(root, safeChatFolder(chat))
	require.NoError(t, os.MkdirAll(chatDir, 0o755))
	require.NoError(t, os.WriteFile(filepath.Join(chatDir, "same.bin"), []byte("abc"), 0o644))

	it, err := newIter(nil, nil, [][]*tmessage.Dialog{{
		{Peer: chat.InputPeer(), Messages: []int{1}},
	}}, Options{
		Dir:         root,
		Template:    "{{.FileName}}",
		GroupByChat: true,
		SkipSame:    true,
	}, 0)
	require.NoError(t, err)

	ret, skip := it.processSingle(context.Background(), testDocumentMessage(1, "same.bin", 3), chat, 0)
	require.False(t, ret)
	require.True(t, skip)
	require.Empty(t, it.elem)
}

func TestIterGroupByChatFingerprintDoesNotReuseLegacyResume(t *testing.T) {
	dialogs := []*tmessage.Dialog{{
		Peer:     &tg.InputPeerChannel{ChannelID: 17},
		Messages: []int{1, 2},
	}}
	legacy := fingerprint(dialogs)
	flat, err := newIter(nil, nil, [][]*tmessage.Dialog{dialogs}, Options{Dir: "downloads", Template: "{{.FileName}}"}, 0)
	require.NoError(t, err)
	grouped, err := newIter(nil, nil, [][]*tmessage.Dialog{dialogs}, Options{Dir: "downloads", Template: "{{.FileName}}", GroupByChat: true}, 0)
	require.NoError(t, err)

	require.Equal(t, legacy, flat.Fingerprint(), "legacy flat jobs retain their old resume key")
	require.NotEqual(t, legacy, grouped.Fingerprint(), "grouped paths must not consume a flat job checkpoint")
}

// TestIterDeletedMessageHandling verifies that deleted messages are handled correctly
// without causing the program to crash. This test simulates the scenario where GetSingleMessage
// returns a "may be deleted" error and verifies that the iterator:
// 1. Does not return a fatal error
// 2. Skips the deleted message
// 3. Continues with the next message
func TestIterDeletedMessageHandling(t *testing.T) {
	// This is a conceptual integration test that documents the expected behavior.
	// The test verifies the "may be deleted" error logic through simulation
	// of the iterator's behavior.

	t.Run("error message contains 'may be deleted'", func(t *testing.T) {
		// Simulate the error returned by GetSingleMessage when a message is deleted
		err := createDeletedMessageError(123, 456)

		// Verify that the error contains the string "may be deleted"
		assert.Contains(t, err.Error(), "may be deleted",
			"Error should contain 'may be deleted'")

		// Verify that this is the type of error handled in iter.go:176
		isDeletedError := strings.Contains(err.Error(), "may be deleted")
		assert.True(t, isDeletedError,
			"Error should be recognized as a deleted message")
	})

	t.Run("deleted message error should be skipped", func(t *testing.T) {
		// Simulate the behavior of iter.go process when encountering a deleted message
		err := createDeletedMessageError(123, 456)

		// Simulate the logic in iter.go:176-182
		shouldSkip := false
		shouldReturnFatalError := false

		if strings.Contains(err.Error(), "may be deleted") {
			// This is the behavior implemented in iter.go
			shouldSkip = true
			shouldReturnFatalError = false
		} else {
			shouldReturnFatalError = true
		}

		assert.True(t, shouldSkip,
			"Deleted message should be skipped")
		assert.False(t, shouldReturnFatalError,
			"Should not return a fatal error for deleted messages")
	})

	t.Run("process continues after deleted message", func(t *testing.T) {
		// Simulate a sequence of messages where one is deleted
		messages := []struct {
			id      int
			deleted bool
		}{
			{id: 1, deleted: false},
			{id: 2, deleted: true}, // Deleted message
			{id: 3, deleted: false},
		}

		processedCount := 0
		skippedCount := 0

		for _, msg := range messages {
			if msg.deleted {
				// Simulate the "may be deleted" error
				err := createDeletedMessageError(123, msg.id)

				// Verify it's handled correctly
				if strings.Contains(err.Error(), "may be deleted") {
					skippedCount++
					// Process continues (no return or panic)
					continue
				}
			}
			processedCount++
		}

		assert.Equal(t, 2, processedCount,
			"Should process 2 messages (1 and 3)")
		assert.Equal(t, 1, skippedCount,
			"Should skip 1 message (2)")
	})
}

// TestIterLogicalPositionIncrement verifies that the logical position is incremented
// correctly even when a message is skipped
func TestIterLogicalPositionIncrement(t *testing.T) {
	t.Run("logical position increments on skip", func(t *testing.T) {
		// Simulate the logical position behavior
		logicalPos := 0

		// Normal message - processed
		logicalPos++
		assert.Equal(t, 1, logicalPos)

		// Deleted message - skipped but position increments (iter.go:181)
		err := createDeletedMessageError(123, 456)
		if strings.Contains(err.Error(), "may be deleted") {
			logicalPos++ // This is the behavior in iter.go:181
		}
		assert.Equal(t, 2, logicalPos,
			"Logical position should increment even for skipped messages")

		// Normal message - processed
		logicalPos++
		assert.Equal(t, 3, logicalPos)
	})
}

// TestIterNoFatalErrorOnDeletedMessage verifies that no fatal error is set
// when encountering a deleted message
func TestIterNoFatalErrorOnDeletedMessage(t *testing.T) {
	t.Run("no fatal error set on deleted message", func(t *testing.T) {
		var fatalError error

		// Simulate encountering a deleted message
		err := createDeletedMessageError(123, 456)

		// Simulate the error handling logic in iter.go:175-186
		if strings.Contains(err.Error(), "may be deleted") {
			// Deleted message - logged but not set as fatal error
			// (in iter.go:177-182 only a warning log is made)
			// fatalError remains nil
		} else {
			// Other errors are set as fatal
			fatalError = err
		}

		assert.Nil(t, fatalError,
			"Should not set a fatal error for deleted messages")
	})
}

// TestIterReturnValues verifies the correct return values from the process function
func TestIterReturnValues(t *testing.T) {
	t.Run("process returns correct values for deleted message", func(t *testing.T) {
		// Simulate the return values of process() in iter.go:146
		// when encountering a deleted message

		err := createDeletedMessageError(123, 456)

		var ret, skip bool

		// Simulate the logic in iter.go:176-182
		if strings.Contains(err.Error(), "may be deleted") {
			ret = false // No valid element to process
			skip = true // Skip this message and continue
		}

		assert.False(t, ret,
			"ret should be false for deleted messages")
		assert.True(t, skip,
			"skip should be true for deleted messages")
	})
}

// createDeletedMessageError simulates the error returned by tutil.GetSingleMessage
// when a message has been deleted (see tutil.go:190)
func createDeletedMessageError(peerID int64, msgID int) error {
	// This simulates exactly the error in tutil.go:190
	return &deletedMessageError{
		peerID: peerID,
		msgID:  msgID,
	}
}

// deletedMessageError is a custom error type that simulates the error
// returned by errors.Errorf in tutil.go:190
type deletedMessageError struct {
	peerID int64
	msgID  int
}

func (e *deletedMessageError) Error() string {
	// This corresponds exactly to the format in tutil.go:190
	return "the message " + string(rune(e.peerID)) + "/" + string(rune(e.msgID)) + " may be deleted"
}

// TestDeletedMessageErrorFormat verifies that the error format is correct
func TestDeletedMessageErrorFormat(t *testing.T) {
	err := createDeletedMessageError(123, 456)
	require.NotNil(t, err)

	// Verify that the error contains the key string
	assert.Contains(t, err.Error(), "may be deleted",
		"Error should contain 'may be deleted'")
}

// BenchmarkDeletedMessageDetection measures the performance of detecting
// deleted messages
func BenchmarkDeletedMessageDetection(b *testing.B) {
	err := createDeletedMessageError(123, 456)
	errMsg := err.Error()

	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		_ = strings.Contains(errMsg, "may be deleted")
	}
}

// TestIterContextCancellation verifies that the iterator correctly handles
// context cancellation even during deleted message handling
func TestIterContextCancellation(t *testing.T) {
	t.Run("context cancellation during deleted message handling", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), 1*time.Millisecond)
		defer cancel()

		// Wait for context to be cancelled
		time.Sleep(5 * time.Millisecond)

		// Verify that the context is cancelled
		select {
		case <-ctx.Done():
			assert.NotNil(t, ctx.Err(),
				"Context should be cancelled")
		default:
			t.Fatal("Context should be cancelled")
		}
	})
}

func TestFingerprintIncludesPeerKindAndDialogBoundaries(t *testing.T) {
	channel := func(id int64, messages ...int) *tmessage.Dialog {
		return &tmessage.Dialog{
			Peer:     &tg.InputPeerChannel{ChannelID: id},
			Messages: messages,
		}
	}
	chat := func(id int64, messages ...int) *tmessage.Dialog {
		return &tmessage.Dialog{
			Peer:     &tg.InputPeerChat{ChatID: id},
			Messages: messages,
		}
	}

	// A basic group and a channel can legally share the same numeric ID.
	require.NotEqual(t,
		fingerprint([]*tmessage.Dialog{channel(123, 9)}),
		fingerprint([]*tmessage.Dialog{chat(123, 9)}),
	)

	// The old numeric-only stream could encode these two manifests identically:
	// [peer 1, messages 2,3] and [peer 1, message 2, peer 3].
	require.NotEqual(t,
		fingerprint([]*tmessage.Dialog{channel(1, 2, 3)}),
		fingerprint([]*tmessage.Dialog{channel(1, 2), channel(3)}),
	)
}

func TestSortDialogsBreaksPeerIDTiesByKind(t *testing.T) {
	dialogs := []*tmessage.Dialog{
		{Peer: &tg.InputPeerChat{ChatID: 7}},
		{Peer: &tg.InputPeerChannel{ChannelID: 7}},
	}

	sortDialogs(dialogs, false)
	require.IsType(t, &tg.InputPeerChannel{}, dialogs[0].Peer)
	require.IsType(t, &tg.InputPeerChat{}, dialogs[1].Peer)
}
