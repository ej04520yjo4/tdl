package chat

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/expr-lang/expr"
	"github.com/go-faster/errors"
	"github.com/gotd/td/telegram"
	"github.com/gotd/td/telegram/message/peer"
	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/telegram/query/dialogs"
	"github.com/gotd/td/tg"
	"github.com/mattn/go-runewidth"
	"go.uber.org/zap"

	"github.com/iyear/tdl/core/logctx"
	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/core/util/tutil"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/texpr"
)

//go:generate go-enum --names --values --flag --nocase

type Dialog struct {
	ID          int64   `json:"id" comment:"ID of dialog"`
	Type        string  `json:"type" comment:"Type of dialog. Can be 'private', 'channel' or 'group'"`
	VisibleName string  `json:"visible_name,omitempty" comment:"Title of channel and group, first and last name of user. If empty, output '-'"`
	Username    string  `json:"username,omitempty" comment:"Username of dialog. If empty, output '-'"`
	Topics      []Topic `json:"topics,omitempty" comment:"Topics of dialog. If not set, output '-'"`
	forum       bool
	// ref is the typed peer reference used by the desktop protocol. Keep it
	// private so the historical CLI JSON/filter field set stays unchanged.
	ref string
}

type Topic struct {
	ID    int    `json:"id" comment:"ID of topic"`
	Title string `json:"title" comment:"Title of topic"`
}

// ListOutput
// ENUM(table, json)
type ListOutput int

// External designation, different from Telegram mtproto
const (
	DialogGroup   = "group"
	DialogPrivate = "private"
	DialogChannel = "channel"
	DialogUnknown = "unknown"
)

type ListOptions struct {
	Output        ListOutput
	Filter        string
	IncludeTopics bool
}

func List(ctx context.Context, c *telegram.Client, kvd storage.Storage, opts ListOptions) error {
	log := logctx.From(ctx)
	gui := desktopio.From(ctx) != nil

	// align output
	runewidth.EastAsianWidth = false
	runewidth.DefaultCondition.EastAsianWidth = false

	// output available fields
	if opts.Filter == "-" {
		fg := texpr.NewFieldsGetter(nil)
		fields, err := fg.Walk(&Dialog{})
		if err != nil {
			return fmt.Errorf("failed to walk fields: %w", err)
		}

		fmt.Print(fg.Sprint(fields, true))
		return nil
	}
	// compile filter
	filter, err := expr.Compile(opts.Filter, expr.AsBool())
	if err != nil {
		return fmt.Errorf("failed to compile filter: %w", err)
	}

	if gui {
		_ = desktopio.Emit(ctx, desktopio.Event{
			Event:   "status",
			Kind:    "chat_scan",
			Stage:   "scanning",
			Message: "正在掃描頻道與群組…",
		})
	}

	// Manually iterate through dialogs to handle errors gracefully.  A failed
	// page is returned as an error instead of looking like the end of the list.
	dialogs, skipped, fetchErr := fetchDialogsWithErrorHandling(ctx, c.API(), func(current int) {
		if gui {
			_ = desktopio.Emit(ctx, desktopio.Event{
				Event:   "status",
				Kind:    "chat_scan",
				Stage:   "scanning",
				Current: int64(current),
				Message: fmt.Sprintf("已掃描 %d 個對話", current),
			})
		}
	})
	if skipped > 0 {
		log.Warn("skipped problematic dialogs during iteration",
			zap.Int("skipped", skipped),
			zap.Int("fetched", len(dialogs)))
	}
	if fetchErr != nil {
		log.Warn("dialog list is partial", zap.Error(fetchErr), zap.Int("fetched", len(dialogs)))
	}

	blocked, err := tutil.GetBlockedDialogs(ctx, c.API())
	if err != nil {
		return err
	}

	manager := peers.Options{Storage: storage.NewPeers(kvd)}.Build(c.API())
	result := make([]*Dialog, 0, len(dialogs))
	var partialMessages []string
	if fetchErr != nil {
		partialMessages = append(partialMessages, desktopio.SafeError(fetchErr))
	}
	for _, d := range dialogs {
		id := tutil.GetInputPeerID(d.Peer)

		// we can update our access hash state if there is any new peer.
		if err = applyPeers(ctx, manager, d.Entities, id); err != nil {
			log.Warn("failed to apply peer updates", zap.Int64("id", id), zap.Error(err))
		}

		// Telegram's blocked-dialog query is keyed by numeric IDs. Apply it
		// only to users so a blocked user cannot hide a channel/basic group
		// which happens to share that numeric ID.
		_, blockedUser := d.Peer.(*tg.InputPeerUser)
		if blockedUser {
			if _, ok := blocked[id]; !ok {
				blockedUser = false
			}
		}
		if blockedUser {
			continue
		}

		var (
			r       *Dialog
			procErr error
		)
		switch t := d.Peer.(type) {
		case *tg.InputPeerUser:
			r = processUser(t.UserID, d.Entities)
		case *tg.InputPeerChannel:
			// Topic enumeration is opt-in for GUI calls: a large account can have
			// hundreds of forums and Telegram may rate-limit one request per forum.
			// The forum marker is emitted even when topics are not requested so the
			// desktop can fetch/require them before allowing a topic-scoped export.
			r, procErr = processChannel(ctx, c.API(), t.ChannelID, d.Entities, !gui || opts.IncludeTopics)
		case *tg.InputPeerChat:
			r = processChat(t.ChatID, d.Entities)
		}
		if procErr != nil {
			partialMessages = append(partialMessages, desktopio.SafeError(procErr))
		}

		// skip unsupported types
		if r == nil {
			continue
		}

		// filter
		b, err := texpr.Run(filter, r)
		if err != nil {
			return fmt.Errorf("failed to run filter: %w", err)
		}
		if !b.(bool) {
			continue
		}

		result = append(result, r)
		if gui {
			_ = desktopio.Emit(ctx, desktopio.Event{
				Event:   "status",
				Kind:    "chat_scan",
				Stage:   "reading",
				Current: int64(len(result)),
				Total:   int64(len(dialogs)),
				Message: fmt.Sprintf("正在整理頻道與群組 %d/%d", len(result), len(dialogs)),
			})
		}
	}

	partial := len(partialMessages) > 0
	if gui {
		items := make([]desktopio.Chat, 0, len(result))
		for _, r := range result {
			topics := make([]desktopio.Topic, 0, len(r.Topics))
			for _, topic := range r.Topics {
				topics = append(topics, desktopio.Topic{ID: topic.ID, Title: topic.Title})
			}
			username := r.Username
			if username == "-" {
				username = ""
			}
			items = append(items, desktopio.Chat{
				ID:       strconv.FormatInt(r.ID, 10),
				Ref:      r.ref,
				Type:     r.Type,
				Title:    r.VisibleName,
				Username: username,
				Forum:    r.forum,
				Topics:   topics,
			})
		}
		skippedCopy := skipped
		event := desktopio.Event{
			Event:   "chats",
			Chats:   &items,
			Skipped: &skippedCopy,
			Partial: partial,
		}
		if partial {
			event.Message = strings.Join(partialMessages, "; ")
		}
		_ = desktopio.Emit(ctx, event)
	} else {
		switch opts.Output {
		case ListOutputTable:
			printTable(result)
		case ListOutputJson:
			bytes, err := json.MarshalIndent(result, "", "\t")
			if err != nil {
				return fmt.Errorf("marshal json: %w", err)
			}

			fmt.Println(string(bytes))
		default:
			return fmt.Errorf("unknown output: %s", opts.Output)
		}
	}

	if partial && !gui {
		return fmt.Errorf("partial chat list: %s", strings.Join(partialMessages, "; "))
	}

	return nil
}

func printTable(result []*Dialog) {
	fmt.Printf("%s %s %s %s %s\n",
		trunc("ID", 10),
		trunc("Type", 8),
		trunc("VisibleName", 20),
		trunc("Username", 20),
		"Topics")

	for _, r := range result {
		fmt.Printf("%s %s %s %s %s\n",
			trunc(strconv.FormatInt(r.ID, 10), 10),
			trunc(r.Type, 8),
			trunc(r.VisibleName, 20),
			trunc(r.Username, 20),
			topicsString(r.Topics))
	}
}

func trunc(s string, len int) string {
	s = strings.TrimSpace(s)
	if s == "" {
		s = "-"
	}

	return runewidth.FillRight(runewidth.Truncate(s, len, "..."), len)
}

func topicsString(topics []Topic) string {
	if len(topics) == 0 {
		return "-"
	}

	s := make([]string, 0, len(topics))
	for _, t := range topics {
		s = append(s, fmt.Sprintf("%d: %s", t.ID, t.Title))
	}

	return strings.Join(s, ", ")
}

func processUser(id int64, entities peer.Entities) *Dialog {
	u, ok := entities.User(id)
	if !ok {
		return nil
	}

	return &Dialog{
		ID:          u.ID,
		VisibleName: visibleName(u.FirstName, u.LastName),
		Username:    u.Username,
		Type:        DialogPrivate,
		Topics:      nil,
		ref:         fmt.Sprintf("user:%d", u.ID),
	}
}

func processChannel(ctx context.Context, api *tg.Client, id int64, entities peer.Entities, includeTopics bool) (*Dialog, error) {
	c, ok := entities.Channel(id)
	if !ok {
		return nil, nil
	}

	d := &Dialog{
		ID:          c.ID,
		VisibleName: c.Title,
		Username:    c.Username,
		forum:       c.Forum,
	}

	// channel type
	switch {
	case c.Broadcast:
		d.Type = DialogChannel
	case c.Megagroup, c.Gigagroup:
		d.Type = DialogGroup
	default:
		d.Type = DialogUnknown
	}
	d.ref = fmt.Sprintf("channel:%d", c.ID)

	if c.Forum && includeTopics {
		topics, err := fetchTopics(ctx, api, c.AsInputPeer())
		if err != nil {
			logctx.From(ctx).Error("failed to fetch topics",
				zap.Int64("channel_id", c.ID),
				zap.String("channel_username", c.Username),
				zap.Error(err))
			// Keep the channel visible even when its topic endpoint fails. The
			// caller marks the overall list partial instead of swallowing it.
			return d, errors.Wrap(err, "fetch topics")
		}

		d.Topics = ensureGeneralTopic(topics)
	}

	return d, nil
}

// ensureGeneralTopic keeps Telegram's non-deletable General forum topic
// selectable even when getForumTopics omits it from the returned page.
func ensureGeneralTopic(topics []Topic) []Topic {
	for _, topic := range topics {
		if topic.ID == 1 {
			return topics
		}
	}
	result := make([]Topic, 0, len(topics)+1)
	result = append(result, Topic{ID: 1, Title: "General"})
	return append(result, topics...)
}

// fetchTopics https://github.com/telegramdesktop/tdesktop/blob/4047f1733decd5edf96d125589f128758b68d922/Telegram/SourceFiles/data/data_forum.cpp#L135
func fetchTopics(ctx context.Context, api *tg.Client, peer tg.InputPeerClass) ([]Topic, error) {
	log := logctx.From(ctx)
	res := make([]Topic, 0)
	limit := 100 // why can't we use 500 like tdesktop?
	offsetTopic, offsetID, offsetDate := 0, 0, 0
	lastOffsetTopic := -1 // Track the last offsetTopic to detect infinite loops

	// Track seen offsetTopics to detect cycles
	seenOffsets := make(map[int]bool)

	for {
		// Detect infinite loop: if offsetTopic hasn't changed or we've seen it before
		if offsetTopic == lastOffsetTopic && lastOffsetTopic != -1 {
			log.Warn("pagination stuck (same offset), breaking loop",
				zap.Int("offset_topic", offsetTopic))
			return res, errors.Errorf("forum topic pagination stuck at offset %d", offsetTopic)
		}
		if seenOffsets[offsetTopic] {
			log.Warn("pagination cycle detected, breaking loop",
				zap.Int("offset_topic", offsetTopic))
			return res, errors.Errorf("forum topic pagination cycle at offset %d", offsetTopic)
		}
		seenOffsets[offsetTopic] = true
		lastOffsetTopic = offsetTopic

		req := &tg.MessagesGetForumTopicsRequest{
			Peer:        peer,
			Limit:       limit,
			OffsetTopic: offsetTopic,
			OffsetID:    offsetID,
			OffsetDate:  offsetDate,
		}

		topics, err := api.MessagesGetForumTopics(ctx, req)
		if err != nil {
			return nil, errors.Wrap(err, "get forum topics")
		}

		// If no topics returned, we're done
		if len(topics.Topics) == 0 {
			break
		}

		for _, tp := range topics.Topics {
			if t, ok := tp.(*tg.ForumTopic); ok {
				res = append(res, Topic{
					ID:    t.ID,
					Title: t.Title,
				})

				offsetTopic = t.ID
			}
		}

		// Safety break if we've collected all topics
		if len(res) >= topics.Count {
			break
		}

		// last page
		if len(topics.Topics) < limit {
			break
		}

		// Update offset using last message if available
		// Use a local variable for length to be absolutely safe against index out of range
		msgCount := len(topics.Messages)
		if msgCount > 0 {
			if lastMsg, ok := topics.Messages[msgCount-1].AsNotEmpty(); ok {
				offsetID, offsetDate = lastMsg.GetID(), lastMsg.GetDate()
			} else {
				log.Debug("no valid message for offset, relying on offsetTopic only",
					zap.Int("offset_topic", offsetTopic))
			}
		} else {
			log.Debug("no messages in topics response, relying on offsetTopic only",
				zap.Int("offset_topic", offsetTopic),
				zap.Int("topics_count", len(topics.Topics)))
		}
	}

	return res, nil
}

func processChat(id int64, entities peer.Entities) *Dialog {
	c, ok := entities.Chat(id)
	if !ok {
		return nil
	}

	return &Dialog{
		ID:          c.ID,
		VisibleName: c.Title,
		Username:    "-",
		Type:        DialogGroup,
		Topics:      nil,
		ref:         fmt.Sprintf("chat:%d", c.ID),
	}
}

func typedPeerRef(p tg.PeerClass) string {
	switch p := p.(type) {
	case *tg.PeerUser:
		return fmt.Sprintf("user:%d", p.UserID)
	case *tg.PeerChat:
		return fmt.Sprintf("chat:%d", p.ChatID)
	case *tg.PeerChannel:
		return fmt.Sprintf("channel:%d", p.ChannelID)
	default:
		return ""
	}
}

type dialogMessageKey struct {
	Peer string
	ID   int
}

func indexDialogMessages(messages []tg.MessageClass) map[dialogMessageKey]tg.NotEmptyMessage {
	indexed := make(map[dialogMessageKey]tg.NotEmptyMessage, len(messages))
	for _, msg := range messages {
		var notEmpty tg.NotEmptyMessage
		switch m := msg.(type) {
		case *tg.Message:
			notEmpty = m
		case *tg.MessageService:
			notEmpty = m
		default:
			continue
		}

		peer := typedPeerRef(notEmpty.GetPeerID())
		if peer == "" {
			continue
		}
		indexed[dialogMessageKey{Peer: peer, ID: notEmpty.GetID()}] = notEmpty
	}
	return indexed
}

func visibleName(first, last string) string {
	if first == "" && last == "" {
		return ""
	}

	if first == "" {
		return last
	}

	if last == "" {
		return first
	}

	return first + " " + last
}

func applyPeers(ctx context.Context, manager *peers.Manager, entities peer.Entities, id int64) error {
	users := make([]tg.UserClass, 0, 1)
	if user, ok := entities.User(id); ok {
		users = append(users, user)
	}

	chats := make([]tg.ChatClass, 0, 1)
	if chat, ok := entities.Chat(id); ok {
		chats = append(chats, chat)
	}
	if channel, ok := entities.Channel(id); ok {
		chats = append(chats, channel)
	}

	return manager.Apply(ctx, users, chats)
}

// fetchDialogsWithErrorHandling manually iterates through dialogs using the raw Telegram API
// to gracefully handle errors from problematic dialogs (deleted/inaccessible channels).
// Instead of failing completely when ExtractPeer fails in gotd's iterator, it logs errors
// and continues, skipping bad dialogs.
func fetchDialogsWithErrorHandling(ctx context.Context, api *tg.Client, progress func(current int)) ([]dialogs.Elem, int, error) {
	log := logctx.From(ctx)
	const batchSize = 100
	var (
		allElems   []dialogs.Elem
		skipped    int
		offsetID   int
		offsetDate int
		offsetPeer tg.InputPeerClass = &tg.InputPeerEmpty{}
		seen                         = make(map[string]bool) // Track typed peers; channel/chat IDs can collide.
		seenPages                    = make(map[string]bool)
	)

	for {
		// Fetch a batch of dialogs using raw API
		result, err := api.MessagesGetDialogs(ctx, &tg.MessagesGetDialogsRequest{
			OffsetDate: offsetDate,
			OffsetID:   offsetID,
			OffsetPeer: offsetPeer,
			Limit:      batchSize,
		})
		if err != nil {
			log.Error("failed to fetch dialog batch", zap.Error(err))
			return allElems, skipped, errors.Wrap(err, "get dialogs")
		}

		var (
			dialogsSlice []tg.DialogClass
			messages     []tg.MessageClass
			users        []tg.UserClass
			chats        []tg.ChatClass
		)

		switch d := result.(type) {
		case *tg.MessagesDialogs:
			dialogsSlice = d.Dialogs
			messages = d.Messages
			users = d.Users
			chats = d.Chats
		case *tg.MessagesDialogsSlice:
			dialogsSlice = d.Dialogs
			messages = d.Messages
			users = d.Users
			chats = d.Chats
		case *tg.MessagesDialogsNotModified:
			// No more dialogs
			return allElems, skipped, nil
		default:
			log.Error("unexpected dialog type", zap.String("type", fmt.Sprintf("%T", result)))
			return allElems, skipped, fmt.Errorf("unexpected dialogs response %T", result)
		}

		if len(dialogsSlice) == 0 {
			return allElems, skipped, nil
		}

		// Build entities map for this batch
		// Convert slices to maps as required by peer.NewEntities
		userMap := make(map[int64]*tg.User)
		for _, u := range users {
			if user, ok := u.(*tg.User); ok {
				userMap[user.ID] = user
			}
		}

		chatMap := make(map[int64]*tg.Chat)
		channelMap := make(map[int64]*tg.Channel)
		for _, c := range chats {
			switch chat := c.(type) {
			case *tg.Chat:
				chatMap[chat.ID] = chat
			case *tg.Channel:
				channelMap[chat.ID] = chat
			}
		}

		entities := peer.NewEntities(userMap, chatMap, channelMap)

		// Message IDs are only unique within a peer. Index by typed peer and
		// message ID so two dialogs with the same numeric message ID cannot
		// exchange their last-message date or pagination offset.
		messageMap := indexDialogMessages(messages)

		// Process each dialog in this batch
		for _, d := range dialogsSlice {
			dialog, ok := d.(*tg.Dialog)
			if !ok {
				continue
			}

			// Find the peer ID for logging purposes
			var peerID int64
			switch p := dialog.Peer.(type) {
			case *tg.PeerUser:
				peerID = p.UserID
			case *tg.PeerChat:
				peerID = p.ChatID
			case *tg.PeerChannel:
				peerID = p.ChannelID
			default:
				log.Error("unknown peer type", zap.String("type", fmt.Sprintf("%T", p)))
				skipped++
				continue
			}

			// Skip if we've already seen this typed dialog (deduplication).
			// Numeric channel and basic-group IDs can collide.
			peerKey := typedPeerRef(dialog.Peer)
			if peerKey == "" {
				log.Error("unknown peer type", zap.String("type", fmt.Sprintf("%T", dialog.Peer)))
				skipped++
				continue
			}
			if seen[peerKey] {
				continue
			}
			seen[peerKey] = true

			// Try to extract the peer - THIS IS WHERE THE ORIGINAL ERROR OCCURS
			// In gotd's query/dialogs iterator, it calls ExtractPeer without error handling,
			// causing a panic when a channel doesn't exist in entities.
			// We catch it here and skip the problematic dialog instead.
			// See: https://github.com/iyear/tdl/issues/713
			inputPeer, err := entities.ExtractPeer(dialog.Peer)
			if err != nil {
				// This dialog references a channel/chat that doesn't exist in entities
				// (likely deleted, user was banned, or channel is inaccessible).
				// Log and skip it instead of failing.
				log.Warn("skipping dialog with missing peer",
					zap.Int64("peer_id", peerID),
					zap.String("peer_type", fmt.Sprintf("%T", dialog.Peer)),
					zap.Error(err))
				skipped++
				continue
			}

			// Get the last message for this dialog from the typed message map.
			lastMsg := messageMap[dialogMessageKey{Peer: peerKey, ID: dialog.TopMessage}]

			// Successfully processed this dialog
			allElems = append(allElems, dialogs.Elem{
				Peer:     inputPeer,
				Entities: entities,
				Dialog:   dialog,
				Last:     lastMsg,
			})
		}
		if progress != nil {
			progress(len(allElems))
		}

		// Update offset for next batch using the last dialog in dialogsSlice
		// (regardless of whether it was successfully processed or skipped)
		if len(dialogsSlice) > 0 {
			lastDialog, ok := dialogsSlice[len(dialogsSlice)-1].(*tg.Dialog)
			if !ok {
				return allElems, skipped, fmt.Errorf("unexpected dialog element %T", dialogsSlice[len(dialogsSlice)-1])
			}
			// Get the message date from the typed message map. A non-empty top
			// message without a matching peer is not safe to treat as date zero:
			// doing so can restart pagination and silently omit later dialogs.
			var msgDate int
			lastPeerKey := typedPeerRef(lastDialog.Peer)
			if lastPeerKey == "" {
				return allElems, skipped, fmt.Errorf("unsupported dialog peer %T for pagination", lastDialog.Peer)
			}
			lastMsg, found := messageMap[dialogMessageKey{Peer: lastPeerKey, ID: lastDialog.TopMessage}]
			if found {
				msgDate = lastMsg.GetDate()
			} else if lastDialog.TopMessage != 0 {
				return allElems, skipped, fmt.Errorf(
					"missing last message %s/%d for dialog pagination",
					lastPeerKey, lastDialog.TopMessage,
				)
			}
			pageKey := fmt.Sprintf("%d:%d:%s", msgDate, lastDialog.TopMessage, typedPeerRef(lastDialog.Peer))
			if seenPages[pageKey] {
				return allElems, skipped, fmt.Errorf("dialog pagination made no progress at %s", pageKey)
			}
			seenPages[pageKey] = true

			offsetDate = msgDate
			offsetID = lastDialog.TopMessage

			// Set offset peer based on dialog peer type.
			// Try to get access hash from entities if available.
			switch peerType := lastDialog.Peer.(type) {
			case *tg.PeerUser:
				if user, ok := entities.User(peerType.UserID); ok {
					offsetPeer = &tg.InputPeerUser{
						UserID:     peerType.UserID,
						AccessHash: user.AccessHash,
					}
				} else {
					// Can't continue pagination without access hash
					log.Error("failed to get user for offset, stopping pagination",
						zap.Int64("user_id", peerType.UserID))
					return allElems, skipped, fmt.Errorf("failed to get user for offset: %d", peerType.UserID)
				}
			case *tg.PeerChat:
				offsetPeer = &tg.InputPeerChat{ChatID: peerType.ChatID}
			case *tg.PeerChannel:
				if channel, ok := entities.Channel(peerType.ChannelID); ok {
					offsetPeer = &tg.InputPeerChannel{
						ChannelID:  peerType.ChannelID,
						AccessHash: channel.AccessHash,
					}
				} else {
					// Can't continue pagination without access hash
					log.Error("failed to get channel for offset, stopping pagination",
						zap.Int64("channel_id", peerType.ChannelID))
					return allElems, skipped, fmt.Errorf("failed to get channel for offset: %d", peerType.ChannelID)
				}
			}
		}

		// Check if we've fetched all dialogs
		// Continue fetching if we got a full batch (there might be more)
		if len(dialogsSlice) < batchSize {
			// Got less than requested, we've reached the end
			return allElems, skipped, nil
		}
	}
}
