package up

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestWalkExtensionFiltersAreCaseInsensitive(t *testing.T) {
	root := t.TempDir()
	for name, contents := range map[string]string{
		"clip.MP4":   "video",
		"photo.JpG":  "photo",
		"notes.TxT":  "text",
		"clip.thumb": "thumbnail",
	} {
		require.NoError(t, os.WriteFile(filepath.Join(root, name), []byte(contents), 0o600))
	}

	files, err := walk([]string{root}, []string{"mp4", "jpg"}, []string{"txt"})
	require.NoError(t, err)
	require.Len(t, files, 2)

	byName := make(map[string]*File, len(files))
	for _, file := range files {
		byName[filepath.Base(file.File)] = file
	}
	video, ok := byName["clip.MP4"]
	require.True(t, ok, "lowercase include should match uppercase file extension")
	require.Equal(t, filepath.Join(root, "clip.thumb"), video.Thumb,
		"thumbnail discovery should keep the original file and thumbnail paths")
	require.Contains(t, byName, "photo.JpG",
		"lowercase include should match mixed-case file extension")
	require.NotContains(t, byName, "notes.TxT",
		"lowercase exclude should match mixed-case file extension")
	require.NotContains(t, byName, "clip.thumb",
		"thumbnail sidecars must remain excluded")
}

func TestWalkExcludeFilterIsCaseInsensitive(t *testing.T) {
	root := t.TempDir()
	for name := range map[string]struct{}{
		"clip.MP4":       {},
		"clip-mixed.Mp4": {},
		"photo.JpG":      {},
		"notes.TxT":      {},
		"clip.thumb":     {},
	} {
		require.NoError(t, os.WriteFile(filepath.Join(root, name), []byte(name), 0o600))
	}

	for _, setting := range []string{"mp4", "MP4"} {
		t.Run(setting, func(t *testing.T) {
			files, err := walk([]string{root}, nil, []string{setting})
			require.NoError(t, err)

			byName := make(map[string]struct{}, len(files))
			for _, file := range files {
				byName[filepath.Base(file.File)] = struct{}{}
			}
			require.NotContains(t, byName, "clip.MP4")
			require.NotContains(t, byName, "clip-mixed.Mp4")
			require.Contains(t, byName, "photo.JpG")
			require.Contains(t, byName, "notes.TxT")
			require.NotContains(t, byName, "clip.thumb")
		})
	}
}

func TestWalkNormalizesFilterSettingsToo(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "clip.mp4")
	require.NoError(t, os.WriteFile(path, []byte("video"), 0o600))

	files, err := walk([]string{root}, []string{"MP4"}, nil)
	require.NoError(t, err)
	require.Len(t, files, 1)
	require.Equal(t, path, files[0].File)
}
