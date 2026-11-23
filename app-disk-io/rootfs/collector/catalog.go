package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"
)

const catalogLimit = 1 << 20

type nameCatalog struct {
	url, token string
	client     *http.Client

	mu          sync.Mutex
	refreshing  bool
	next        time.Time
	lastSuccess int64
	// Published maps are immutable so formatting never holds up a refresh.
	names map[string]string
}

func coreNames() map[string]string {
	return map[string]string{
		"homeassistant":     "Home Assistant Core",
		"hassio_supervisor": "Supervisor",
		"hassio_dns":        "Home Assistant DNS",
		"hassio_audio":      "Home Assistant Audio",
		"hassio_cli":        "Home Assistant CLI",
		"hassio_observer":   "Home Assistant Observer",
		"hassio_multicast":  "Home Assistant Multicast",
	}
}

func newNameCatalog(token string) *nameCatalog {
	return &nameCatalog{
		url:   "http://supervisor/supervisor/info",
		token: token,
		names: coreNames(),
		client: &http.Client{
			Timeout:       4 * time.Second,
			Transport:     &http.Transport{IdleConnTimeout: 30 * time.Second},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

func (c *nameCatalog) fetch(ctx context.Context) (map[string]string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.url, nil)
	if err != nil {
		return nil, errors.New("invalid Supervisor URL")
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	resp, err := c.client.Do(req)
	if err != nil {
		return nil, errors.New("Supervisor unavailable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Supervisor returned HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, catalogLimit+1))
	if err != nil || len(body) > catalogLimit {
		return nil, errors.New("unreadable or oversized Supervisor response")
	}
	var result struct {
		Result string
		Data   struct{ Addons []struct{ Slug, Name string } }
	}
	if json.Unmarshal(body, &result) != nil || result.Result != "ok" || result.Data.Addons == nil {
		return nil, errors.New("invalid Supervisor catalog")
	}
	names := coreNames()
	for _, app := range result.Data.Addons {
		slug, name := strings.TrimSpace(app.Slug), strings.TrimSpace(app.Name)
		if slug == "" || name == "" {
			continue
		}
		key := "app_" + slug
		if previous, ok := names[key]; ok && previous != name {
			return nil, errors.New("conflicting names for one Supervisor slug")
		}
		names[key] = name
	}
	// Two repositories may publish different apps under the same title.
	counts := make(map[string]int, len(names))
	for _, name := range names {
		counts[name]++
	}
	for key, name := range names {
		if counts[name] > 1 {
			names[key] = fmt.Sprintf("%s (%s)", name, strings.TrimPrefix(key, "app_"))
		}
	}
	return names, nil
}

func (c *nameCatalog) refresh(ctx context.Context) {
	names, err := c.fetch(ctx)
	c.mu.Lock()
	defer c.mu.Unlock()
	c.refreshing = false
	if err != nil {
		c.next = time.Now().Add(time.Minute)
		return
	}
	c.names = names
	c.lastSuccess = time.Now().Unix()
	c.next = time.Now().Add(10 * time.Minute)
}

func (c *nameCatalog) appendSample(ctx context.Context, sample []byte, at time.Time) []byte {
	c.mu.Lock()
	names := c.names
	lastSuccess := c.lastSuccess
	if c.token != "" && !c.refreshing && !time.Now().Before(c.next) {
		c.refreshing = true
		go c.refresh(ctx)
	}
	c.mu.Unlock()
	sample = fmt.Appendf(sample, "ha_container_io_catalog_last_success_timestamp_seconds %d %d\n", lastSuccess, at.UnixMilli())
	keys := make([]string, 0, len(names))
	for key := range names {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		sample = fmt.Appendf(sample, "ha_container_io_name_info{container_name=%s,display_name=%s} 1 %d\n", promQuote(key), promQuote(names[key]), at.UnixMilli())
	}
	return sample
}
