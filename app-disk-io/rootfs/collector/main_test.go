package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestAccountingGapsAndLabels(t *testing.T) {
	snapshot := ioSnapshot{Discovered: 3, Containers: []containerIO{
		{ID: "abc", Name: "app_core_samba", Read: 12345, Written: 876, ReadOps: 45, ReadOpsValid: true},
		{ID: "def", Read: 678, Written: 5},
	}}
	got := string(render(snapshot, time.UnixMilli(1001), true))
	for _, line := range []string{
		`ha_container_io_read_bytes_total{container_name="app_core_samba",container_id="abc"} 12345 1001`,
		`ha_container_io_write_bytes_total{container_name="",container_id="def"} 5 1001`,
		`ha_container_io_read_operations_total{container_name="app_core_samba",container_id="abc"} 45 1001`,
		"ha_container_io_discovered 3 1001", "ha_container_io_accounted 2 1001", "ha_container_io_named 1 1001",
	} {
		if !strings.Contains(got, line+"\n") {
			t.Fatalf("missing sample %s", line)
		}
	}
	if strings.Contains(got, "write_operations_total") {
		t.Fatal("unavailable operations were reported as zero")
	}
	partial := string(render(snapshot, time.UnixMilli(1001), false))
	if !strings.Contains(partial, "ha_container_io_collection_success 0 1001") || !strings.Contains(partial, "ha_container_io_accounted 2 1001") {
		t.Fatalf("partial counters lost on a later scope timeout: %s", partial)
	}
	failed := string(render(ioSnapshot{}, time.UnixMilli(1001), false))
	if failed != "ha_container_io_collection_success 0 1001\n" {
		t.Fatalf("failed collection emitted totals: %s", failed)
	}
}

func TestUploadPreservesObservationTimes(t *testing.T) {
	want := "metric 1 1000\nmetric 2 2000\n"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		data, err := io.ReadAll(r.Body)
		if err != nil || string(data) != want || r.Method != http.MethodPost {
			t.Errorf("unexpected batch %q", data)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	if err := upload(context.Background(), server.Client(), server.URL, [][]byte{[]byte("metric 1 1000\n"), []byte("metric 2 2000\n")}); err != nil {
		t.Fatal(err)
	}
}
