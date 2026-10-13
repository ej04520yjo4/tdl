package login

import (
	"context"
	"encoding/base64"
	"fmt"
	"strings"

	"github.com/AlecAivazis/survey/v2"
	"github.com/fatih/color"
	"github.com/go-faster/errors"
	"github.com/gotd/td/telegram/auth/qrlogin"
	"github.com/gotd/td/tg"
	"github.com/gotd/td/tgerr"
	"github.com/jedib0t/go-pretty/v6/text"
	"github.com/skip2/go-qrcode"
	"github.com/spf13/viper"

	"github.com/iyear/tdl/pkg/consts"
	"github.com/iyear/tdl/pkg/desktopio"
	"github.com/iyear/tdl/pkg/key"
	"github.com/iyear/tdl/pkg/kv"
	"github.com/iyear/tdl/pkg/tclient"
)

func QR(ctx context.Context) error {
	kvd, err := kv.From(ctx).Open(viper.GetString(consts.FlagNamespace))
	if err != nil {
		return errors.Wrap(err, "open kv")
	}

	if err = kvd.Set(ctx, key.App(), []byte(tclient.AppDesktop)); err != nil {
		return errors.Wrap(err, "set app")
	}

	d := tg.NewUpdateDispatcher()

	c, err := tclient.New(ctx, tclient.Options{
		KV:               kvd,
		Proxy:            viper.GetString(consts.FlagProxy),
		NTP:              viper.GetString(consts.FlagNTP),
		ReconnectTimeout: viper.GetDuration(consts.FlagReconnectTimeout),
		UpdateHandler:    d,
	}, true)
	if err != nil {
		return errors.Wrap(err, "create client")
	}

	return c.Run(ctx, func(ctx context.Context) error {
		gui := desktopio.From(ctx) != nil
		if !gui {
			color.Blue("Scan QR code with your Telegram app...")
		}

		var lines int
		_, err = c.QR().Auth(ctx, qrlogin.OnLoginToken(d), func(ctx context.Context, token qrlogin.Token) error {
			qr, err := qrcode.New(token.URL(), qrcode.Medium)
			if err != nil {
				return errors.Wrap(err, "create qr")
			}
			if gui {
				png, err := qr.PNG(512)
				if err != nil {
					return errors.Wrap(err, "encode qr")
				}
				return desktopio.Emit(ctx, desktopio.Event{
					Event: "qr",
					PNG:   base64.StdEncoding.EncodeToString(png),
				})
			}
			code := qr.ToSmallString(false)
			lines = strings.Count(code, "\n")

			fmt.Print(code)
			fmt.Print(strings.Repeat(text.CursorUp.Sprint(), lines))
			return nil
		})

		if !gui {
			// clear qrcode
			out := &strings.Builder{}
			for i := 0; i < lines; i++ {
				out.WriteString(text.EraseLine.Sprint())
				out.WriteString(text.CursorDown.Sprint())
			}
			out.WriteString(text.CursorUp.Sprintn(lines))
			fmt.Print(out.String())
		}

		if err != nil {
			// https://core.telegram.org/api/auth#2fa
			if !tgerr.Is(err, "SESSION_PASSWORD_NEEDED") {
				return errors.Wrap(err, "qr auth")
			}

			var pwd string
			if gui {
				pwd, err = desktopio.Prompt(ctx, "password", "請輸入 Telegram 兩步驟驗證密碼：")
			} else {
				prompt := &survey.Password{
					Message: "Enter 2FA Password:",
				}

				err = survey.AskOne(prompt, &pwd, survey.WithValidator(survey.Required))
			}
			if err != nil {
				return errors.Wrap(err, "2fa password")
			}

			if _, err = c.Auth().Password(ctx, pwd); err != nil {
				return errors.Wrap(err, "2fa auth")
			}
		}

		user, err := c.Self(ctx)
		if err != nil {
			return errors.Wrap(err, "get self")
		}

		if gui {
			_ = desktopio.Emit(ctx, desktopio.Event{
				Event:    "authenticated",
				ID:       fmt.Sprintf("%d", user.ID),
				Username: user.Username,
				Name:     userDisplayName(user),
			})
		} else {
			fmt.Print(text.EraseLine.Sprint())
			color.Green("Login successfully! ID: %d, Username: %s", user.ID, user.Username)
		}
		return nil
	})
}
