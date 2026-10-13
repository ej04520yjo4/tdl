package chat

import (
	"testing"

	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
	"github.com/stretchr/testify/require"
)

func TestIndexDialogMessagesSeparatesPeersWithSameMessageID(t *testing.T) {
	channelMessage := &tg.Message{
		ID:     42,
		PeerID: &tg.PeerChannel{ChannelID: 100},
		Date:   1000,
	}
	chatMessage := &tg.Message{
		ID:     42,
		PeerID: &tg.PeerChat{ChatID: 100},
		Date:   2000,
	}

	indexed := indexDialogMessages([]tg.MessageClass{channelMessage, chatMessage})
	require.Same(t, channelMessage, indexed[dialogMessageKey{Peer: "channel:100", ID: 42}])
	require.Same(t, chatMessage, indexed[dialogMessageKey{Peer: "chat:100", ID: 42}])
	require.Len(t, indexed, 2)
}

func TestEnsureGeneralTopicAddsTelegramGeneral(t *testing.T) {
	got := ensureGeneralTopic([]Topic{{ID: 9, Title: "公告"}})
	require.Equal(t, []Topic{{ID: 1, Title: "General"}, {ID: 9, Title: "公告"}}, got)
}

func TestEnsureGeneralTopicDoesNotDuplicateTelegramGeneral(t *testing.T) {
	input := []Topic{{ID: 1, Title: "一般"}, {ID: 9, Title: "公告"}}
	got := ensureGeneralTopic(input)
	require.Equal(t, input, got)
}

func TestCanonicalChatRef(t *testing.T) {
	manager := (&peers.Options{}).Build(nil)
	channel := manager.Channel(&tg.Channel{ID: 987})
	require.Equal(t, "channel:987", canonicalChatRef(channel))
}
