package main

import (
	"context"
	"os"
	"os/signal"

	surveyterm "github.com/AlecAivazis/survey/v2/terminal"
	"github.com/fatih/color"
	"github.com/go-faster/errors"
	"go.etcd.io/bbolt"

	"github.com/iyear/tdl/cmd"
	"github.com/iyear/tdl/pkg/desktopio"
)

func main() {
	root, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	ctx, bridge := desktopio.Start(root)
	if bridge != nil {
		defer bridge.Close()
	}

	humanizeErrors := map[error]string{
		bbolt.ErrTimeout:        "Current database is used by another process, please terminate it first",
		surveyterm.InterruptErr: "Interrupted",
	}

	if err := cmd.New().ExecuteContext(ctx); err != nil {
		if bridge != nil {
			// Cancellation is a normal GUI action: downloads have already saved
			// their resume checkpoint in their deferred cleanup path.
			if !errors.Is(err, context.Canceled) {
				_ = bridge.Emit(desktopio.Event{
					Event:   "error",
					Message: desktopio.SafeError(err),
				})
				os.Exit(1)
			}
			return
		}
		for e, m := range humanizeErrors {
			if errors.Is(err, e) {
				color.Red("%s", m)
				os.Exit(1)
			}
		}

		color.Red("Error: %+v", err)
		os.Exit(1)
	}
}
