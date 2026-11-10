package dl

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"

	"github.com/go-faster/errors"
	"github.com/gotd/td/tg"

	"github.com/iyear/tdl/core/downloader"
	"github.com/iyear/tdl/core/tmedia"
)

// partExt names the part list kept next to a paused temporary file, for
// example "1_2_video.mp4.tmp.part".
const partExt = ".part"

// partFile is the part list on disk.  It is written only after a graceful
// pause has flushed the temporary file, so every listed part is complete.
type partFile struct {
	Version  int     `json:"version"`
	Key      string  `json:"key"`
	Size     int64   `json:"size"`
	PartSize int64   `json:"part_size"`
	Parts    []int64 `json:"parts"`
}

// partState tracks which parts of one file are on disk.
type partState struct {
	key  string
	size int64

	mu   sync.Mutex
	done map[int64]struct{}
}

// locationKey identifies the Telegram file behind a location; "" means the
// file kind is not resumable.
func locationKey(location tg.InputFileLocationClass) string {
	switch l := location.(type) {
	case *tg.InputDocumentFileLocation:
		return fmt.Sprintf("document:%d:%s", l.ID, l.ThumbSize)
	case *tg.InputPhotoFileLocation:
		return fmt.Sprintf("photo:%d:%s", l.ID, l.ThumbSize)
	default:
		return ""
	}
}

// openTemp opens the temporary download file.  When a paused download left
// a matching part list, the file is reopened as it is and the listed parts
// are not downloaded again.  Otherwise the file starts empty.
//
// The part list stays on disk while the download continues: the parts it
// lists are never rewritten, so after a crash it still describes the file.
func openTemp(path string, media *tmedia.Media) (*os.File, *partState, error) {
	state := &partState{
		key:  locationKey(media.InputFileLoc),
		size: media.Size,
		done: make(map[int64]struct{}),
	}
	if parts, ok := readParts(path, state.key, state.size); ok {
		if f, err := os.OpenFile(path, os.O_RDWR, 0o644); err == nil {
			for _, index := range parts {
				state.done[index] = struct{}{}
			}
			return f, state, nil
		}
	}

	// Starting over: a stale part list must never describe the new file.
	if err := os.Remove(path + partExt); err != nil && !os.IsNotExist(err) {
		return nil, nil, errors.Wrap(err, "remove old part list")
	}
	f, err := os.Create(path)
	if err != nil {
		return nil, nil, err
	}
	return f, state, nil
}

// readParts returns the parts listed for path when the list belongs to the
// same Telegram file and the temporary file really holds them.
func readParts(path, key string, size int64) ([]int64, bool) {
	if key == "" || size <= 0 {
		return nil, false
	}
	raw, err := os.ReadFile(path + partExt)
	if err != nil {
		return nil, false
	}
	var list partFile
	if err := json.Unmarshal(raw, &list); err != nil ||
		list.Version != 1 || list.Key != key || list.Size != size || list.PartSize != downloader.MaxPartSize {
		return nil, false
	}
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > size {
		return nil, false
	}

	count := (size + downloader.MaxPartSize - 1) / downloader.MaxPartSize
	parts := make([]int64, 0, len(list.Parts))
	for _, index := range list.Parts {
		end := (index + 1) * downloader.MaxPartSize
		if end > size {
			end = size
		}
		if index < 0 || index >= count || end > info.Size() {
			return nil, false
		}
		parts = append(parts, index)
	}
	return parts, len(parts) > 0
}

// add records a part written to disk.
func (s *partState) add(index int64) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.done[index] = struct{}{}
}

// parts returns the parts on disk in order.
func (s *partState) parts() []int64 {
	if s == nil {
		return nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	parts := make([]int64, 0, len(s.done))
	for index := range s.done {
		parts = append(parts, index)
	}
	sort.Slice(parts, func(a, b int) bool { return parts[a] < parts[b] })
	return parts
}

// resumable reports whether a pause can keep this file's parts.
func (s *partState) resumable() bool {
	return s != nil && s.key != "" && s.size > 0 && len(s.parts()) > 0
}

// save writes the part list for the temporary file at path.  The caller
// flushes the file first; the list is replaced atomically.
func (s *partState) save(path string) error {
	raw, err := json.Marshal(partFile{
		Version:  1,
		Key:      s.key,
		Size:     s.size,
		PartSize: downloader.MaxPartSize,
		Parts:    s.parts(),
	})
	if err != nil {
		return err
	}
	pending := path + partExt + ".new"
	if err := writeSynced(pending, raw); err != nil {
		_ = os.Remove(pending)
		return err
	}
	return os.Rename(pending, path+partExt)
}

func writeSynced(path string, data []byte) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		_ = f.Close()
		return err
	}
	return f.Close()
}
