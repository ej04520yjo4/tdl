package consts

import (
	"os"
	"path/filepath"
	"strings"
)

func init() {
	dir, err := os.UserHomeDir()
	if err != nil {
		panic(err)
	}

	HomeDir = dir
	DataDir = filepath.Join(dir, ".tdl")
	// The desktop front end starts one isolated process per remembered account.
	// Keep the normal CLI location stable, while allowing that front end to
	// choose a private data root for each account.  Restricting the override to
	// GUI mode prevents an unrelated shell environment variable from silently
	// moving an existing CLI installation.
	if guiDataDir := strings.TrimSpace(os.Getenv("TDL_DATA_DIR")); guiDataDir != "" && guiEnabled() {
		DataDir = guiDataDir
	}
	LogPath = filepath.Join(DataDir, "log")
	ExtensionsPath = filepath.Join(DataDir, "extensions")
	ExtensionsDataPath = filepath.Join(ExtensionsPath, "data")

	for _, p := range []string{DataDir, ExtensionsPath, ExtensionsDataPath} {
		if err = os.MkdirAll(p, 0o755); err != nil {
			panic(err)
		}
	}
}

func guiEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("TDL_GUI"))) {
	case "1", "true", "yes":
		return true
	default:
		return false
	}
}
