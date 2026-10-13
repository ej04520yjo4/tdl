package dl

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/gotd/td/telegram/peers"
	"github.com/gotd/td/tg"
	"github.com/stretchr/testify/require"

	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/pkg/key"
	"github.com/iyear/tdl/pkg/tmessage"
)

type folderTestStorage struct {
	data     map[string][]byte
	getErr   error
	setErr   error
	setCalls int
}

func (s *folderTestStorage) Get(_ context.Context, key string) ([]byte, error) {
	if s.getErr != nil {
		return nil, s.getErr
	}
	value, ok := s.data[key]
	if !ok {
		return nil, storage.ErrNotFound
	}
	return append([]byte(nil), value...), nil
}

func (s *folderTestStorage) Set(_ context.Context, key string, value []byte) error {
	s.setCalls++
	if s.setErr != nil {
		return s.setErr
	}
	if s.data == nil {
		s.data = make(map[string][]byte)
	}
	s.data[key] = append([]byte(nil), value...)
	return nil
}

func (s *folderTestStorage) Delete(_ context.Context, key string) error {
	delete(s.data, key)
	return nil
}

func newGroupedFolderIter(t *testing.T, root string, peer tg.InputPeerClass) *iter {
	t.Helper()
	it, err := newIter(nil, nil, [][]*tmessage.Dialog{{
		{Peer: peer, Messages: []int{1}},
	}}, Options{Dir: root, Template: "{{.FileName}}", GroupByChat: true}, 0)
	require.NoError(t, err)
	return it
}

func TestChatFolderMappingKeepsRenamedPeerFolder(t *testing.T) {
	root := t.TempDir()
	store := &folderTestStorage{}
	manager := (&peers.Options{}).Build(nil)

	first := manager.Chat(&tg.Chat{ID: 31, Title: "Original"})
	it := newGroupedFolderIter(t, root, first.InputPeer())
	require.NoError(t, it.configureChatFolders(context.Background(), store))

	ret, skip := it.processSingle(context.Background(), testDocumentMessage(1, "first.bin", 1), first, 0)
	require.True(t, ret)
	require.False(t, skip)
	firstElem := (<-it.elem).(*iterElem)
	require.Equal(t, filepath.Join(root, "Original", "first.bin"+tempExt), firstElem.to.Name())
	closeAndRemoveIterElem(t, firstElem)
	require.Equal(t, 1, store.setCalls)

	renamed := manager.Chat(&tg.Chat{ID: 31, Title: "Renamed"})
	resumed := newGroupedFolderIter(t, root, renamed.InputPeer())
	require.NoError(t, resumed.configureChatFolders(context.Background(), store))
	ret, skip = resumed.processSingle(context.Background(), testDocumentMessage(2, "second.bin", 1), renamed, 0)
	require.True(t, ret)
	require.False(t, skip)
	secondElem := (<-resumed.elem).(*iterElem)
	require.Equal(t, filepath.Join(root, "Original", "second.bin"+tempExt), secondElem.to.Name())
	closeAndRemoveIterElem(t, secondElem)
	require.Equal(t, 1, store.setCalls, "a resumed peer uses its saved folder without rewriting metadata")
}

func TestChatFolderMappingReservesFinishedPeerCollision(t *testing.T) {
	root := t.TempDir()
	store := &folderTestStorage{}
	rootKey, err := normalizeChatOutputRoot(root)
	require.NoError(t, err)
	require.NoError(t, persistChatFolders(context.Background(), store, rootKey, map[string]string{
		"chat:41": "Shared",
	}))

	manager := (&peers.Options{}).Build(nil)
	channel := manager.Channel(&tg.Channel{ID: 42, Title: "Shared"})
	it := newGroupedFolderIter(t, root, channel.InputPeer())
	require.NoError(t, it.configureChatFolders(context.Background(), store))

	// The chat:41 source can be fully finished and skipped on resume. Its
	// persisted ownership must still force a new peer into a separate folder.
	output, err := it.outputDir(context.Background(), channel)
	require.NoError(t, err)
	require.Equal(t, filepath.Join(root, "Shared (channel-42)"), output)

	// A real title may already equal a previously allocated suffixed folder.
	// The next sequence must still terminate with a distinct safe folder.
	other := manager.User(&tg.User{ID: 43, FirstName: "Shared (channel-42)"})
	output, err = it.outputDir(context.Background(), other)
	require.NoError(t, err)
	require.Equal(t, filepath.Join(root, "Shared (channel-42) (user-43)"), output)
}

func TestChatFolderMappingCaseOnlyCollisionOnWindows(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("Windows path comparison semantics")
	}
	root := t.TempDir()
	store := &folderTestStorage{}
	rootKey, err := normalizeChatOutputRoot(root)
	require.NoError(t, err)
	require.NoError(t, persistChatFolders(context.Background(), store, rootKey, map[string]string{
		"chat:91": "Shared",
	}))

	manager := (&peers.Options{}).Build(nil)
	peer := manager.Channel(&tg.Channel{ID: 92, Title: "shared"})
	it := newGroupedFolderIter(t, root, peer.InputPeer())
	require.NoError(t, it.configureChatFolders(context.Background(), store))
	output, err := it.outputDir(context.Background(), peer)
	require.NoError(t, err)
	require.Equal(t, filepath.Join(root, "shared (channel-92)"), output)
}

func TestChatFolderMappingIsScopedToOutputRoot(t *testing.T) {
	rootOne := t.TempDir()
	rootTwo := t.TempDir()
	store := &folderTestStorage{}
	keyOne, err := normalizeChatOutputRoot(rootOne)
	require.NoError(t, err)
	require.NoError(t, persistChatFolders(context.Background(), store, keyOne, map[string]string{
		"channel:51": "Shared",
	}))

	manager := (&peers.Options{}).Build(nil)
	peer := manager.Channel(&tg.Channel{ID: 52, Title: "Shared"})
	it := newGroupedFolderIter(t, rootTwo, peer.InputPeer())
	require.NoError(t, it.configureChatFolders(context.Background(), store))

	output, err := it.outputDir(context.Background(), peer)
	require.NoError(t, err)
	require.Equal(t, filepath.Join(rootTwo, "Shared"), output)
}

func TestGroupedFingerprintIncludesOutputRoot(t *testing.T) {
	rootOne := t.TempDir()
	rootTwo := t.TempDir()
	peer := &tg.InputPeerChannel{ChannelID: 53}
	dialogs := [][]*tmessage.Dialog{{{Peer: peer, Messages: []int{1}}}}
	first, err := newIter(nil, nil, dialogs, Options{Dir: rootOne, Template: "{{.FileName}}", GroupByChat: true}, 0)
	require.NoError(t, err)
	second, err := newIter(nil, nil, dialogs, Options{Dir: rootTwo, Template: "{{.FileName}}", GroupByChat: true}, 0)
	require.NoError(t, err)
	require.NotEqual(t, first.Fingerprint(), second.Fingerprint())
}

func TestChatFolderMappingWriteFailurePreventsFileCreation(t *testing.T) {
	root := t.TempDir()
	writeErr := errors.New("storage write failed")
	store := &folderTestStorage{setErr: writeErr}
	manager := (&peers.Options{}).Build(nil)
	peer := manager.Chat(&tg.Chat{ID: 61, Title: "Cannot Save"})
	it := newGroupedFolderIter(t, root, peer.InputPeer())
	require.NoError(t, it.configureChatFolders(context.Background(), store))

	ret, skip := it.processSingle(context.Background(), testDocumentMessage(1, "must-not-exist.bin", 1), peer, 0)
	require.False(t, ret)
	require.False(t, skip)
	require.ErrorIs(t, it.Err(), writeErr)
	entries, err := os.ReadDir(root)
	require.NoError(t, err)
	require.Empty(t, entries, "mapping failure must happen before directories or files are created")
}

func TestChatFolderMappingReadFailureStopsConfiguration(t *testing.T) {
	readErr := errors.New("storage read failed")
	store := &folderTestStorage{getErr: readErr}
	root := t.TempDir()
	manager := (&peers.Options{}).Build(nil)
	peer := manager.Chat(&tg.Chat{ID: 71, Title: "Cannot Read"})
	it := newGroupedFolderIter(t, root, peer.InputPeer())
	require.ErrorIs(t, it.configureChatFolders(context.Background(), store), readErr)
	require.Nil(t, it.folderStore)
}

func TestChatFolderMappingRejectsUnsafeStoredFolder(t *testing.T) {
	root := t.TempDir()
	rootKey, err := normalizeChatOutputRoot(root)
	require.NoError(t, err)
	encoded, err := json.Marshal(chatFolderManifest{
		Version: chatFolderManifestVersion,
		Folders: map[string]string{"channel:81": "..\\escape"},
	})
	require.NoError(t, err)
	store := &folderTestStorage{data: map[string][]byte{key.ChatFolders(rootKey): encoded}}
	manager := (&peers.Options{}).Build(nil)
	peer := manager.Channel(&tg.Channel{ID: 81, Title: "Unsafe"})
	it := newGroupedFolderIter(t, root, peer.InputPeer())

	require.Error(t, it.configureChatFolders(context.Background(), store))
	require.Nil(t, it.folderStore)
}
