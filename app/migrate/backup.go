package migrate

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/fatih/color"
	"github.com/go-faster/errors"
	"github.com/klauspost/compress/zstd"
	"go.uber.org/multierr"

	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/kv"
)

// These small seams keep the commit path testable without making the backup
// format or the storage implementation configurable in production.
var (
	backupCreateTemp = os.CreateTemp
	backupRename     = os.Rename
	backupRemove     = os.Remove
	backupSync       = func(f *os.File) error { return f.Sync() }
	backupClose      = func(f *os.File) error { return f.Close() }
	backupEncode     = encodeBackup
)

func Backup(ctx context.Context, dst string) (rerr error) {
	meta, err := kv.From(ctx).MigrateTo()
	if err != nil {
		return errors.Wrap(err, "read metadata")
	}
	if err = ctx.Err(); err != nil {
		return errors.Wrap(err, "backup canceled")
	}

	metaB, err := json.Marshal(meta)
	if err != nil {
		return errors.Wrap(err, "marshal metadata")
	}

	dir := filepath.Dir(dst)
	base := filepath.Base(dst)
	temp, err := backupCreateTemp(dir, fmt.Sprintf(".%s-*.tmp", base))
	if err != nil {
		return errors.Wrap(err, "create temporary backup")
	}
	tempName := temp.Name()
	closed := false
	committed := false
	defer func() {
		if !closed {
			if closeErr := backupClose(temp); closeErr != nil {
				rerr = multierr.Combine(rerr, errors.Wrap(closeErr, "close temporary backup"))
			}
		}
		if !committed {
			if removeErr := backupRemove(tempName); removeErr != nil && !os.IsNotExist(removeErr) {
				rerr = multierr.Combine(rerr, errors.Wrap(removeErr, "remove temporary backup"))
			}
		}
	}()

	if err = backupEncode(temp, metaB); err != nil {
		return errors.Wrap(err, "write backup")
	}

	if err = backupSync(temp); err != nil {
		return errors.Wrap(err, "sync temporary backup")
	}
	if err = backupClose(temp); err != nil {
		closed = true
		return errors.Wrap(err, "close temporary backup")
	}
	closed = true

	if err = ctx.Err(); err != nil {
		return errors.Wrap(err, "backup canceled")
	}
	if err = replaceBackup(tempName, dst); err != nil {
		return err
	}
	committed = true

	if desktopio.From(ctx) != nil {
		_ = desktopio.Emit(ctx, desktopio.Event{Event: "status", Kind: "backup", Message: "備份資料已寫入"})
		return nil
	}
	color.Green("Backup successfully, file: %s", dst)
	return nil
}

func encodeBackup(w io.Writer, meta []byte) error {
	enc, err := zstd.NewWriter(w, zstd.WithEncoderLevel(zstd.SpeedBestCompression))
	if err != nil {
		return errors.Wrap(err, "create zstd encoder")
	}
	_, writeErr := enc.Write(meta)
	closeErr := enc.Close()
	return multierr.Combine(writeErr, closeErr)
}

// replaceBackup commits the prepared file with one same-directory rename. The
// Go Windows runtime maps this to MoveFileEx with replacement semantics for a
// regular existing file, while a failed rename leaves the old destination
// untouched. Keeping this as one operation also avoids a crash window where a
// destination could be temporarily absent.
func replaceBackup(temp, dst string) error {
	if err := backupRename(temp, dst); err != nil {
		return errors.Wrap(err, "commit backup")
	}
	return nil
}
