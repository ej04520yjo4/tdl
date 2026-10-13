package dl

import (
	"context"
	"encoding/json"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/go-faster/errors"

	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/pkg/key"
)

const chatFolderManifestVersion = 1

type chatFolderManifest struct {
	Version int               `json:"version"`
	Folders map[string]string `json:"folders"`
}

// normalizeChatOutputRoot gives one durable mapping key to equivalent output
// paths. Windows treats path spelling and case as equivalent, so lower-casing
// there avoids duplicate mappings for the same directory.
func normalizeChatOutputRoot(dir string) (string, error) {
	root, err := filepath.Abs(filepath.Clean(dir))
	if err != nil {
		return "", err
	}
	if runtime.GOOS == "windows" {
		root = strings.ToLower(root)
	}
	return root, nil
}

func folderCollisionKey(folder string) string {
	if runtime.GOOS == "windows" {
		return strings.ToLower(folder)
	}
	return folder
}

func loadChatFolders(ctx context.Context, kvd storage.Storage, root string) (map[string]string, error) {
	if kvd == nil {
		return nil, errors.New("chat folder storage is unavailable")
	}

	b, err := kvd.Get(ctx, key.ChatFolders(root))
	if err != nil {
		if errors.Is(err, storage.ErrNotFound) {
			return make(map[string]string), nil
		}
		return nil, errors.Wrap(err, "read chat folder mapping")
	}

	var manifest chatFolderManifest
	if err := json.Unmarshal(b, &manifest); err != nil {
		return nil, errors.Wrap(err, "decode chat folder mapping")
	}
	if manifest.Version != chatFolderManifestVersion {
		return nil, errors.Errorf("unsupported chat folder mapping version %d", manifest.Version)
	}
	if manifest.Folders == nil {
		manifest.Folders = make(map[string]string)
	}

	result := make(map[string]string, len(manifest.Folders))
	used := make(map[string]string, len(manifest.Folders))
	for peerKey, folder := range manifest.Folders {
		if strings.TrimSpace(peerKey) == "" {
			return nil, errors.New("chat folder mapping contains an empty peer key")
		}
		if folder == "" || folder != sanitizeWindowsComponent(folder) {
			return nil, errors.Errorf("chat folder mapping contains unsafe folder %q", folder)
		}
		collisionKey := folderCollisionKey(folder)
		if owner, ok := used[collisionKey]; ok && owner != peerKey {
			return nil, errors.Errorf("chat folder mapping collides for %q", folder)
		}
		result[peerKey] = folder
		used[collisionKey] = peerKey
	}
	return result, nil
}

func persistChatFolders(ctx context.Context, kvd storage.Storage, root string, folders map[string]string) error {
	if kvd == nil {
		return errors.New("chat folder storage is unavailable")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	manifest := chatFolderManifest{
		Version: chatFolderManifestVersion,
		Folders: make(map[string]string, len(folders)),
	}
	for peerKey, folder := range folders {
		manifest.Folders[peerKey] = folder
	}
	b, err := json.Marshal(manifest)
	if err != nil {
		return errors.Wrap(err, "encode chat folder mapping")
	}
	if err := kvd.Set(ctx, key.ChatFolders(root), b); err != nil {
		return errors.Wrap(err, "write chat folder mapping")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	return nil
}

func (i *iter) configureChatFolders(ctx context.Context, kvd storage.Storage) error {
	if !i.opts.GroupByChat {
		return nil
	}
	if i.folderRoot == "" {
		return errors.New("chat folder root is unavailable")
	}
	folders, err := loadChatFolders(ctx, kvd, i.folderRoot)
	if err != nil {
		return err
	}
	i.folderMu.Lock()
	defer i.folderMu.Unlock()
	i.chatFolders = folders
	i.usedFolders = make(map[string]string, len(folders))
	for peerKey, folder := range folders {
		i.usedFolders[folderCollisionKey(folder)] = peerKey
	}
	i.folderStore = kvd
	return nil
}
