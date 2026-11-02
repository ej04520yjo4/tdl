package cmd

import "testing"

func TestForwardHasTypedTopicFlag(t *testing.T) {
	command := NewForward()
	flag := command.Flags().Lookup("topic")
	if flag == nil {
		t.Fatal("forward command must expose --topic")
	}
	if flag.DefValue != "0" {
		t.Fatalf("--topic default = %q, want 0", flag.DefValue)
	}
}
