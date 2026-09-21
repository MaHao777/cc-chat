package core

import "testing"

func TestAICHATQueueQuiet(t *testing.T) {
	for _, mode := range []string{"quiet", "compact", "full"} {
		for _, starting := range []bool{true, false} {
			t.Run(mode+map[bool]string{true: "/startup", false: "/busy"}[starting], func(t *testing.T) {
				p := &stubPlatformEngine{n: "test"}
				e := newTestEngine()
				e.display.Mode = mode
				state := &interactiveState{platform: p, replyCtx: "ctx"}
				if !starting {
					state.agentSession = newQueuingSession("aichat")
				}
				key := "test:owner"
				e.interactiveStates[key] = state
				for _, content := range []string{"first", "second"} {
					if !e.queueMessageForBusySession(p, &Message{
						SessionKey: key, Content: content, ReplyCtx: "ctx",
					}, key) {
						t.Fatal("message was not queued")
					}
				}
				if len(state.pendingMessages) != 2 || state.pendingMessages[0].content != "first" ||
					state.pendingMessages[1].content != "second" {
					t.Fatal("queue lost messages or FIFO order")
				}
				want := 2
				if mode == "quiet" {
					want = 0
				}
				if len(p.getSent()) != want {
					t.Fatalf("got %d receipts, want %d", len(p.getSent()), want)
				}
			})
		}
	}
}
