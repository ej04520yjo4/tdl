package chat

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/peers"

	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/pkg/desktopio"
)

// TopicsOptions controls delayed forum-topic enumeration for one chat.
// Unlike ListOptions, this command always targets one explicitly selected
// chat, avoiding one Telegram request per forum in a large account.
type TopicsOptions struct {
	Chat   string
	Output ListOutput
}

type topicsOutput struct {
	ChatRef string  `json:"chat_ref"`
	Topics  []Topic `json:"topics"`
}

// Topics enumerates the topics of one forum. In GUI mode it emits exactly one
// structured event, including an empty topics array for a forum with no
// visible custom topics. In CLI mode it prints a small JSON object or table.
func Topics(ctx context.Context, c *telegram.Client, kvd storage.Storage, opts TopicsOptions) error {
	if strings.TrimSpace(opts.Chat) == "" {
		return fmt.Errorf("chat reference is required")
	}

	manager := peers.Options{Storage: storage.NewPeers(kvd)}.Build(c.API())
	resolved, err := resolvePeer(ctx, manager, opts.Chat)
	if err != nil {
		return fmt.Errorf("failed to get peer: %w", err)
	}
	channel, ok := resolved.(peers.Channel)
	if !ok {
		return fmt.Errorf("chat %q is not a channel forum", opts.Chat)
	}
	if raw := channel.Raw(); raw == nil || !raw.Forum {
		return fmt.Errorf("chat %q is not a forum", opts.Chat)
	}

	topics, err := fetchTopics(ctx, c.API(), channel.InputPeer())
	if err != nil {
		return fmt.Errorf("fetch topics for %s: %w", canonicalChatRef(channel), err)
	}
	topics = ensureGeneralTopic(topics)
	chatRef := canonicalChatRef(channel)

	if desktopio.From(ctx) != nil {
		items := make([]desktopio.Topic, 0, len(topics))
		for _, topic := range topics {
			items = append(items, desktopio.Topic{ID: topic.ID, Title: topic.Title})
		}
		_ = desktopio.Emit(ctx, desktopio.Event{
			Event:   "topics",
			ChatRef: chatRef,
			Topics:  &items,
		})
		return nil
	}

	switch opts.Output {
	case ListOutputTable:
		fmt.Printf("%s %s\n", trunc("ID", 10), "Title")
		for _, topic := range topics {
			fmt.Printf("%s %s\n", trunc(fmt.Sprintf("%d", topic.ID), 10), topic.Title)
		}
	case ListOutputJson:
		data, err := json.MarshalIndent(topicsOutput{ChatRef: chatRef, Topics: topics}, "", "\t")
		if err != nil {
			return fmt.Errorf("marshal topics json: %w", err)
		}
		fmt.Println(string(data))
	default:
		return fmt.Errorf("unknown output: %s", opts.Output)
	}
	return nil
}

func canonicalChatRef(channel peers.Channel) string {
	return fmt.Sprintf("channel:%d", channel.ID())
}
