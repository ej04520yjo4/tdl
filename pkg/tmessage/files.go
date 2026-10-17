package tmessage

import (
	"context"
	"errors"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/bcicen/jstream"
	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
	"github.com/mitchellh/mapstructure"
	"go.uber.org/zap"

	"github.com/iyear/tdl/core/dcpool"
	"github.com/iyear/tdl/core/logctx"
	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/core/util/tutil"
)

const (
	keyID       = "id"
	keyPeerType = "peer_type"
	typeMessage = "message"
)

type fMessage struct {
	ID     int         `mapstructure:"id"`
	Type   string      `mapstructure:"type"`
	Time   string      `mapstructure:"date_unixtime"`
	File   string      `mapstructure:"file"`
	Photo  string      `mapstructure:"photo"`
	FromID string      `mapstructure:"from_id"`
	From   string      `mapstructure:"from"`
	Text   interface{} `mapstructure:"text"`
}

func FromFile(ctx context.Context, pool dcpool.Pool, kvd storage.Storage, files []string, onlyMedia bool) ParseSource {
	return func() ([]*Dialog, error) {
		dialogs := make([]*Dialog, 0, len(files))

		for _, file := range files {
			d, err := parseFile(ctx, pool.Default(ctx), kvd, file, onlyMedia)
			if err != nil {
				return nil, err
			}

			logctx.From(ctx).Debug("Parse file",
				zap.String("file", file),
				zap.Int("num", len(d.Messages)))
			dialogs = append(dialogs, d)
		}

		return dialogs, nil
	}
}

func parseFile(ctx context.Context, client *tg.Client, kvd storage.Storage, file string, onlyMedia bool) (*Dialog, error) {
	f, err := os.Open(file)
	if err != nil {
		return nil, err
	}
	defer func(f *os.File) {
		_ = f.Close()
	}(f)

	peer, err := getChatInfo(ctx, client, kvd, f)
	if err != nil {
		return nil, err
	}
	logctx.From(ctx).Debug("Got peer info",
		zap.Int64("id", peer.ID()),
		zap.String("name", peer.VisibleName()))

	if _, err = f.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}

	return collect(ctx, f, peer, onlyMedia)
}

func collect(ctx context.Context, r io.Reader, peer peers.Peer, onlyMedia bool) (*Dialog, error) {
	d := jstream.NewDecoder(r, 2)

	m := &Dialog{
		Peer:     peer.InputPeer(),
		Messages: make([]int, 0),
	}

	for mv := range d.Stream() {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		default:
			fm := fMessage{}

			if mv.ValueType != jstream.Object {
				continue
			}

			if err := mapstructure.WeakDecode(mv.Value, &fm); err != nil {
				return nil, err
			}

			if fm.ID < 0 || fm.Type != typeMessage {
				continue
			}

			if fm.File == "" && fm.Photo == "" && onlyMedia {
				continue
			}

			m.Messages = append(m.Messages, fm.ID)
		}
	}

	return m, nil
}

func getChatInfo(ctx context.Context, client *tg.Client, kvd storage.Storage, r io.Reader) (peers.Peer, error) {
	d := jstream.NewDecoder(r, 1).EmitKV()

	chatID := int64(0)
	peerType := ""

	for mv := range d.Stream() {
		_kv, ok := mv.Value.(jstream.KV)
		if !ok {
			continue
		}

		switch _kv.Key {
		case keyID:
			// The first id is the export's top-level peer id. Keep it
			// unchanged while scanning nested message records.
			if chatID == 0 {
				chatID = parseChatID(_kv.Value)
			}
		case keyPeerType:
			if value, ok := _kv.Value.(string); ok {
				peerType = strings.ToLower(strings.TrimSpace(value))
			}
		}

		if chatID != 0 && peerType != "" {
			break
		}
	}

	if chatID == 0 {
		return nil, errors.New("can't get chat type or chat id")
	}

	manager := peers.Options{Storage: storage.NewPeers(kvd)}.Build(client)
	if peerType != "" {
		switch peerType {
		case "channel":
			return manager.ResolveChannelID(ctx, chatID)
		case "chat", "group":
			return manager.ResolveChatID(ctx, chatID)
		case "user", "private":
			return manager.ResolveUserID(ctx, chatID)
		default:
			return nil, errors.New("unsupported peer_type: " + peerType)
		}
	}
	return tutil.GetInputPeer(ctx, manager, strconv.FormatInt(chatID, 10))
}

func parseChatID(value any) int64 {
	switch value := value.(type) {
	case float64:
		return int64(value)
	case float32:
		return int64(value)
	case int:
		return int64(value)
	case int64:
		return value
	case string:
		id, _ := strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		return id
	default:
		return 0
	}
}
