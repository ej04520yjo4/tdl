package cmd

import (
	"context"
	"fmt"
	"math"
	"strings"
	"time"

	"github.com/gotd/contrib/middleware/ratelimit"
	"github.com/gotd/td/telegram"
	"github.com/spf13/cobra"
	"golang.org/x/time/rate"

	"github.com/iyear/tdl/app/chat"
	"github.com/iyear/tdl/core/logctx"
	"github.com/iyear/tdl/core/storage"
)

var limiter = ratelimit.New(rate.Every(500*time.Millisecond), 2)

func NewChat() *cobra.Command {
	cmd := &cobra.Command{
		Use:     "chat",
		Short:   "A set of chat tools",
		GroupID: groupTools.ID,
	}

	cmd.AddCommand(NewChatList(), NewChatTopics(), NewChatExport(), NewChatUsers())

	return cmd
}

func NewChatTopics() *cobra.Command {
	var opts chat.TopicsOptions

	cmd := &cobra.Command{
		Use:   "topics",
		Short: "List topics for one forum",
		RunE: func(cmd *cobra.Command, args []string) error {
			return tRun(cmd.Context(), func(ctx context.Context, c *telegram.Client, kvd storage.Storage) error {
				return chat.Topics(logctx.Named(ctx, "topics"), c, kvd, opts)
			}, limiter)
		},
	}

	cmd.Flags().StringVarP(&opts.Chat, "chat", "c", "", "typed chat reference, for example channel:123")
	cmd.Flags().VarP(&opts.Output, "output", "o", fmt.Sprintf("output format: [%s]", strings.Join(chat.ListOutputNames(), ", ")))
	return cmd
}

func NewChatList() *cobra.Command {
	var opts chat.ListOptions

	cmd := &cobra.Command{
		Use:   "ls",
		Short: "List your chats",
		RunE: func(cmd *cobra.Command, args []string) error {
			return tRun(cmd.Context(), func(ctx context.Context, c *telegram.Client, kvd storage.Storage) error {
				return chat.List(logctx.Named(ctx, "ls"), c, kvd, opts)
			}, limiter)
		},
	}

	cmd.Flags().VarP(&opts.Output, "output", "o", fmt.Sprintf("output format: [%s]", strings.Join(chat.ListOutputNames(), ", ")))
	cmd.Flags().StringVarP(&opts.Filter, "filter", "f", "true", "filter chats by expression")
	cmd.Flags().BoolVar(&opts.IncludeTopics, "topics", false, "include forum topics (GUI: fetch topics before showing the picker)")

	return cmd
}

func NewChatExport() *cobra.Command {
	var opts chat.ExportOptions

	cmd := &cobra.Command{
		Use:   "export",
		Short: "export messages from (protected) chat for download",
		RunE: func(cmd *cobra.Command, args []string) error {
			if err := normalizeExportInput(&opts); err != nil {
				return err
			}

			return tRun(cmd.Context(), func(ctx context.Context, c *telegram.Client, kvd storage.Storage) error {
				return chat.Export(logctx.Named(ctx, "export"), c, kvd, opts)
			}, limiter)
		},
	}

	const (
		_type = "type"
		_chat = "chat"
		input = "input"
	)

	cmd.Flags().VarP(&opts.Type, _type, "T", fmt.Sprintf("export type: [%s]", strings.Join(chat.ExportTypeNames(), ", ")))
	cmd.Flags().StringVarP(&opts.Chat, _chat, "c", "", "chat id or domain. If not specified, 'Saved Messages' will be used")

	cmd.Flags().IntVar(&opts.Topic, "topic", 0, "specify forum topic id (1 is General)")
	cmd.Flags().IntVar(&opts.Reply, "reply", 0, "specify channel post id for its discussion thread")

	cmd.Flags().IntSliceVarP(&opts.Input, input, "i", []int{}, "input data, depends on export type")
	cmd.Flags().StringVarP(&opts.Filter, "filter", "f", "true", "filter messages by expression, defaults to match all messages. Specify '-' to see available fields")
	cmd.Flags().StringVarP(&opts.Output, "output", "o", "tdl-export.json", "output JSON file path")
	cmd.Flags().StringVar(&opts.MediaTypes, "media-types", "", "comma-separated media types: video, photo, audio, document")
	cmd.Flags().BoolVar(&opts.WithContent, "with-content", false, "export with message content")
	cmd.Flags().BoolVar(&opts.Raw, "raw", false, "export raw message struct of Telegram MTProto API, useful for debugging")
	cmd.Flags().BoolVar(&opts.All, "all", false, "export all messages including non-media messages, but still affected by filter and type flag")

	// completion and validation
	_ = cmd.RegisterFlagCompletionFunc(input, func(cmd *cobra.Command, args []string, toComplete string) ([]string, cobra.ShellCompDirective) {
		// if user has already input something, don't do anything
		if toComplete != "" {
			return []string{}, cobra.ShellCompDirectiveNoFileComp
		}

		switch cmd.Flags().Lookup(_type).Value.String() {
		case chat.ExportTypeTime.String():
			return []string{"0,9999999"}, cobra.ShellCompDirectiveNoFileComp
		case chat.ExportTypeId.String():
			return []string{"0,9999999"}, cobra.ShellCompDirectiveNoFileComp
		case chat.ExportTypeLast.String():
			return []string{"100"}, cobra.ShellCompDirectiveNoFileComp
		default:
			return []string{}, cobra.ShellCompDirectiveNoFileComp
		}
	})

	return cmd
}

func normalizeExportInput(opts *chat.ExportOptions) error {
	switch opts.Type {
	case chat.ExportTypeTime, chat.ExportTypeId:
		// Set defaults while keeping the existing inclusive range contract.
		switch len(opts.Input) {
		case 0:
			opts.Input = []int{0, math.MaxInt}
		case 1:
			opts.Input = append(opts.Input, math.MaxInt)
		}

		if len(opts.Input) != 2 {
			return fmt.Errorf("input data should be 2 integers when export type is %s", opts.Type)
		}

		if opts.Input[0] > opts.Input[1] {
			opts.Input[0], opts.Input[1] = opts.Input[1], opts.Input[0]
		}
	case chat.ExportTypeLast:
		if len(opts.Input) != 1 {
			return fmt.Errorf("input data should be 1 integer when export type is %s", opts.Type)
		}
	default:
		return fmt.Errorf("unknown export type: %s", opts.Type)
	}
	return nil
}

func NewChatUsers() *cobra.Command {
	var opts chat.UsersOptions

	cmd := &cobra.Command{
		Use:   "users",
		Short: "export users from (protected) channels",
		RunE: func(cmd *cobra.Command, args []string) error {
			return tRun(cmd.Context(), func(ctx context.Context, c *telegram.Client, kvd storage.Storage) error {
				return chat.Users(logctx.Named(ctx, "users"), c, kvd, opts)
			}, limiter)
		},
	}

	cmd.Flags().StringVarP(&opts.Output, "output", "o", "tdl-users.json", "output JSON file path")
	cmd.Flags().StringVarP(&opts.Chat, "chat", "c", "", "domain id (channels, supergroups, etc.)")
	cmd.Flags().BoolVar(&opts.Raw, "raw", false, "export raw message struct of Telegram MTProto API, useful for debugging")
	return cmd
}
