package cmd

import (
	"context"
	"fmt"
	"strings"

	"github.com/fatih/color"
	"github.com/gotd/td/telegram"
	"github.com/spf13/cobra"

	"github.com/iyear/tdl/core/storage"
	"github.com/iyear/tdl/pkg/desktopio"
)

// NewWhoami reports the account behind the current session. The desktop
// client uses it after `recover` to confirm that a restored session is still
// accepted by Telegram before it marks the account as logged in.
func NewWhoami() *cobra.Command {
	return &cobra.Command{
		Use:     "whoami",
		Short:   "Show the logged in Telegram account",
		GroupID: groupAccount.ID,
		RunE: func(cmd *cobra.Command, args []string) error {
			return tRun(cmd.Context(), func(ctx context.Context, c *telegram.Client, kvd storage.Storage) error {
				user, err := c.Self(ctx)
				if err != nil {
					return err
				}

				name := strings.TrimSpace(user.FirstName + " " + user.LastName)
				if desktopio.From(ctx) != nil {
					return desktopio.Emit(ctx, desktopio.Event{
						Event:    "authenticated",
						ID:       fmt.Sprintf("%d", user.ID),
						Username: user.Username,
						Name:     name,
					})
				}

				color.Green("ID: %d, Username: %s, Name: %s", user.ID, user.Username, name)
				return nil
			})
		},
	}
}
