package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestNameCatalogRefreshAndRetention(t *testing.T) {
	body := make(chan string, 1)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" || r.URL.Path != "/supervisor/info" || r.Header.Get("Authorization") != "Bearer fixture-token" {
			t.Error("incorrect credentials or method")
		}
		io.WriteString(w, <-body)
	}))
	defer srv.Close()
	c := newNameCatalog("fixture-token")
	c.url = strings.Replace(c.url, "http://supervisor", srv.URL, 1)
	ctx := context.Background()
	body <- `{"result":"ok","data":{"addons":[{"slug":"local_one","name":"Weather \"North\"","state":"stopped"},{"slug":"local_one","name":"Weather \"North\""},{"slug":"empty","name":""},{"name":"unmatched"}]}}`
	c.refresh(ctx)
	got := string(c.appendSample(ctx, []byte("raw\n"), time.UnixMilli(123)))
	want := `ha_container_io_name_info{container_name="app_local_one",display_name="Weather \"North\""} 1 123`
	if !strings.HasPrefix(got, "raw\n") || strings.Count(got, want) != 1 || strings.Contains(got, "app_empty") || strings.Contains(got, "unmatched") || !strings.Contains(got, `container_name="homeassistant"`) {
		t.Fatalf("unexpected rows: %s", got)
	}
	if time.Until(c.next) < 9*time.Minute {
		t.Fatal("early successful refresh")
	}
	for _, bad := range []string{`{"result":"ok","data":{}}`, `{"result":"error","data":{"addons":[]}}`, `{"result":"ok","data":{"addons":[{"slug":"dup","name":"A"},{"slug":"dup","name":"B"}]}}`, strings.Repeat(" ", catalogLimit+1)} {
		body <- bad
		c.refresh(ctx)
		if c.names["app_local_one"] != `Weather "North"` || time.Until(c.next) > time.Minute || time.Until(c.next) < 50*time.Second {
			t.Fatal("failed refresh lost names or retry window")
		}
	}
	body <- `{"result":"ok","data":{"addons":[{"slug":"local_one","name":"New"}]}}`
	c.refresh(ctx)
	if c.names["app_local_one"] != "New" {
		t.Fatal("rename missing")
	}
	body <- `{"result":"ok","data":{"addons":[]}}`
	c.refresh(ctx)
	if _, ok := c.names["app_local_one"]; ok {
		t.Fatal("uninstalled app persisted")
	}
}

func TestNameCatalogNeverFollowsRedirect(t *testing.T) {
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("unexpected redirected request") }))
	defer other.Close()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, other.URL, http.StatusFound) }))
	defer srv.Close()
	c := newNameCatalog("fixture-token")
	c.url = srv.URL
	if _, err := c.fetch(context.Background()); err == nil || strings.Contains(fmt.Sprint(err), "fixture-token") {
		t.Fatal("redirect was accepted or disclosed token")
	}
}
