package cmd

import (
	"math"
	"testing"

	"github.com/iyear/tdl/app/chat"
)

func TestNormalizeExportInputDateRange(t *testing.T) {
	opts := chat.ExportOptions{Type: chat.ExportTypeTime, Input: []int{1735689600, 1735775999}}
	if err := normalizeExportInput(&opts); err != nil {
		t.Fatal(err)
	}
	if opts.Input[0] != 1735689600 || opts.Input[1] != 1735775999 {
		t.Fatalf("date range changed: %#v", opts.Input)
	}

	defaults := chat.ExportOptions{Type: chat.ExportTypeTime}
	if err := normalizeExportInput(&defaults); err != nil {
		t.Fatal(err)
	}
	if defaults.Input[0] != 0 || defaults.Input[1] != math.MaxInt {
		t.Fatalf("defaults = %#v", defaults.Input)
	}

	single := chat.ExportOptions{Type: chat.ExportTypeTime, Input: []int{99}}
	if err := normalizeExportInput(&single); err != nil {
		t.Fatal(err)
	}
	if single.Input[0] != 99 || single.Input[1] != math.MaxInt {
		t.Fatalf("single bound = %#v", single.Input)
	}

	reversed := chat.ExportOptions{Type: chat.ExportTypeTime, Input: []int{200, 100}}
	if err := normalizeExportInput(&reversed); err != nil {
		t.Fatal(err)
	}
	if reversed.Input[0] != 100 || reversed.Input[1] != 200 {
		t.Fatalf("reversed range = %#v", reversed.Input)
	}
}

func TestChatTopicsCommandHasDelayedChatFlag(t *testing.T) {
	command, _, err := NewChat().Find([]string{"topics"})
	if err != nil {
		t.Fatal(err)
	}
	if command == nil || command.Name() != "topics" {
		t.Fatalf("topics command = %#v", command)
	}
	if command.Flags().Lookup("chat") == nil || command.Flags().Lookup("output") == nil {
		t.Fatal("topics command must expose --chat and --output")
	}
}
