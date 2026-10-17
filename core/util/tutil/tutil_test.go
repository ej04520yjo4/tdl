package tutil

import "testing"

func TestParseTypedPeer(t *testing.T) {
	cases := []struct {
		in   string
		kind string
		id   int64
		ok   bool
	}{
		{"channel:123", "channel", 123, true},
		{" Channel : 456 ", "channel", 456, true},
		{"chat:7", "chat", 7, true},
		{"group:7", "chat", 7, true},
		{"user:9", "user", 9, true},
		{"private:9", "user", 9, true},
		// Everything below must keep the historical username/ID/link path.
		{"123", "", 0, false},
		{"telegram", "", 0, false},
		{"https://t.me/telegram", "", 0, false},
		{"channel:", "", 0, false},
		{"channel:-5", "", 0, false},
		{"channel:abc", "", 0, false},
		{"bot:5", "", 0, false},
		{"", "", 0, false},
	}
	for _, tc := range cases {
		kind, id, ok := ParseTypedPeer(tc.in)
		if kind != tc.kind || id != tc.id || ok != tc.ok {
			t.Errorf("ParseTypedPeer(%q) = (%q, %d, %v), want (%q, %d, %v)",
				tc.in, kind, id, ok, tc.kind, tc.id, tc.ok)
		}
	}
}
