package migrate

import (
	"context"

	"github.com/AlecAivazis/survey/v2"
	"github.com/fatih/color"
	"github.com/go-faster/errors"

	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/kv"
)

func Migrate(ctx context.Context, to map[string]string) error {
	var confirm bool
	if desktopio.From(ctx) != nil {
		return errors.New("GUI 模式不支援未確認的儲存遷移，請使用命令列完成此操作")
	}
	if err := survey.AskOne(&survey.Confirm{
		Message: "It will overwrite the namespace data in the destination storage, continue?",
		Default: false,
	}, &confirm); err != nil {
		return errors.Wrap(err, "confirm")
	}
	if !confirm {
		return nil
	}

	meta, err := kv.From(ctx).MigrateTo()
	if err != nil {
		return errors.Wrap(err, "read data")
	}

	dest, err := kv.NewWithMap(to)
	if err != nil {
		return errors.Wrap(err, "create dest storage")
	}

	if err = dest.MigrateFrom(meta); err != nil {
		return errors.Wrap(err, "migrate from")
	}

	color.Green("Migrate successfully.")
	for ns := range meta {
		color.Green(" - %s", ns)
	}
	return nil
}
