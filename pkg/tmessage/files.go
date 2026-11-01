package tmessage

import (
	"context"
	"encoding/json"
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
	keyMessages = "messages"
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

	return collect(ctx, f, peer.InputPeer(), onlyMedia)
}

func collect(ctx context.Context, r io.Reader, peer tg.InputPeerClass, onlyMedia bool) (*Dialog, error) {
	d := jstream.NewDecoder(r, 2)

	m := &Dialog{
		Peer:     peer,
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

	// A syntax error ends the stream early; report it instead of
	// downloading only the messages read before it.
	if err := d.Err(); err != nil {
		return nil, err
	}

	return m, nil
}

func getChatInfo(ctx context.Context, client *tg.Client, kvd storage.Storage, r io.Reader) (peers.Peer, error) {
	chatID, peerType, err := readChatHeader(r)
	if err != nil {
		return nil, err
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

// readChatHeader returns the export's top-level id and peer_type, stopping
// at "messages" once the id is known.  It reads synchronously on purpose: a
// streaming decoder left running after an early return kept reading the
// file while the caller re-read it from the start, so the second pass saw
// only part of the messages.  Official Telegram Desktop exports have no
// peer_type, which used to mean decoding the whole messages array here.
func readChatHeader(r io.Reader) (int64, string, error) {
	dec := json.NewDecoder(r)
	dec.UseNumber()
	if tok, err := dec.Token(); err != nil {
		return 0, "", err
	} else if delim, ok := tok.(json.Delim); !ok || delim != '{' {
		return 0, "", errors.New("export file is not a JSON object")
	}

	chatID := int64(0)
	peerType := ""
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return 0, "", err
		}
		key, _ := tok.(string)

		switch key {
		case keyID, keyPeerType:
			var value any
			if err := dec.Decode(&value); err != nil {
				return 0, "", err
			}
			if key == keyID && chatID == 0 {
				chatID = parseChatID(value)
			}
			if text, ok := value.(string); ok && key == keyPeerType {
				peerType = strings.ToLower(strings.TrimSpace(text))
			}
		case keyMessages:
			if chatID != 0 {
				return chatID, peerType, nil
			}
			fallthrough
		default:
			if err := skipJSONValue(dec); err != nil {
				return 0, "", err
			}
		}
	}

	return chatID, peerType, nil
}

// skipJSONValue consumes one value token by token, so skipping a large
// array does not hold it in memory.
func skipJSONValue(dec *json.Decoder) error {
	depth := 0
	for {
		tok, err := dec.Token()
		if err != nil {
			return err
		}
		if delim, ok := tok.(json.Delim); ok {
			switch delim {
			case '{', '[':
				depth++
			case '}', ']':
				depth--
			}
		}
		if depth == 0 {
			return nil
		}
	}
}

func parseChatID(value any) int64 {
	switch value := value.(type) {
	case json.Number:
		if id, err := value.Int64(); err == nil {
			return id
		}
		id, _ := value.Float64()
		return int64(id)
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
