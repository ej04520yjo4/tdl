package migrate

import (
	"bytes"
	"context"
	"encoding/json"
	"os"

	"github.com/fatih/color"
	"github.com/go-faster/errors"
	"github.com/klauspost/compress/zstd"
	"go.uber.org/multierr"

	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/kv"
)

func Recover(ctx context.Context, file string) (rerr error) {
	f, err := os.Open(file)
	if err != nil {
		return errors.Wrap(err, "open file")
	}
	defer multierr.AppendInvoke(&rerr, multierr.Close(f))

	dec, err := zstd.NewReader(f)
	if err != nil {
		return errors.Wrap(err, "create zstd decoder")
	}
	defer dec.Close()

	metaB := bytes.NewBuffer(nil)
	if _, err = dec.WriteTo(metaB); err != nil {
		return err
	}

	var meta kv.Meta
	if err = json.Unmarshal(metaB.Bytes(), &meta); err != nil {
		return errors.Wrap(err, "unmarshal metadata")
	}

	if err = kv.From(ctx).MigrateFrom(meta); err != nil {
		return errors.Wrap(err, "migrate from")
	}

	if desktopio.From(ctx) != nil {
		_ = desktopio.Emit(ctx, desktopio.Event{Event: "status", Kind: "recover", Message: "備份資料已還原"})
		return nil
	}
	color.Green("Recover successfully, file: %s", file)
	return nil
}
