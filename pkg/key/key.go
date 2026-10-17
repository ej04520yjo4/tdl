package key

import (
	"github.com/iyear/tdl/core/storage/keygen"
)

func App() string {
	return keygen.New("app")
}

func Resume(fingerprint string) string {
	return keygen.New("resume", fingerprint)
}

// ChatFolders stores the durable peer-to-output-folder bindings used by the
// opt-in grouped downloader. The root is normalized by the caller before it
// reaches this function so different spellings of one Windows path share a
// mapping.
func ChatFolders(root string) string {
	return keygen.New("chat-folders", root)
}
