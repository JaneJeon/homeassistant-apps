//go:build linux

package main

import (
	"testing"
	"time"
)

func TestLookupBudgetAndMissingJournal(t *testing.T) {
	s := &source{
		names:    map[string]nameEntry{"known": {name: "homeassistant"}},
		requests: make(chan string, namesPerCycle),
		resolved: make(chan nameResult, namesPerCycle),
	}
	rows := []containerIO{{ID: "known"}, {ID: "a"}, {ID: "b"}, {ID: "c"}, {ID: "d"}, {ID: "e"}}
	active := map[string]struct{}{}
	for _, row := range rows {
		active[row.ID] = struct{}{}
	}
	now := time.Unix(100, 0)
	s.applyNames(rows, active, now)
	if len(s.requests) != namesPerCycle || rows[0].Name != "homeassistant" {
		t.Fatal("initial budget or cached name lost")
	}
	// A stalled worker must not prevent rendering a snapshot.
	s.applyNames(rows, active, now.Add(time.Minute))
	for len(s.requests) > 0 {
		<-s.requests
	}
	s.resolved <- nameResult{id: "a", name: "app_a"}
	s.resolved <- nameResult{id: "gone", name: "stale"}
	s.applyNames(rows, active, now.Add(2*time.Minute))
	if rows[1].Name != "app_a" || len(s.requests) != 1 || <-s.requests != "e" {
		t.Fatal("pending identity was starved or retried too soon")
	}
	if _, ok := s.names["gone"]; ok {
		t.Fatal("exited scope resurrected")
	}
}
