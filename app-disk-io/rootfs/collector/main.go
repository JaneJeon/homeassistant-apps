package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"

	"strings"
	"syscall"
	"time"
)

const (
	interval     = time.Minute
	pendingLimit = 60 // Preserve a short backend outage without writing to the monitored disk.
)

type options struct {
	MetricsURL string `json:"metrics_url"`
}

func readOptions(r io.Reader) (options, error) {
	var cfg options
	b, err := io.ReadAll(io.LimitReader(r, 65536))
	if err != nil {
		return cfg, err
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, err
	}
	u, err := url.ParseRequestURI(cfg.MetricsURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return cfg, fmt.Errorf("metrics_url must be an absolute HTTP or HTTPS import URL")
	}
	return cfg, nil
}

func render(s ioSnapshot, at time.Time, ok bool) []byte {
	var b bytes.Buffer
	ms := at.UnixMilli()
	named := 0
	for _, c := range s.Containers {
		if c.Name != "" {
			named++
		}
		labels := "{container_name=" + promQuote(c.Name) + ",container_id=" + promQuote(c.ID) + "}"
		fmt.Fprintf(&b, "ha_container_io_read_bytes_total%s %d %d\n", labels, c.Read, ms)
		fmt.Fprintf(&b, "ha_container_io_write_bytes_total%s %d %d\n", labels, c.Written, ms)
		if c.ReadOpsValid {
			fmt.Fprintf(&b, "ha_container_io_read_operations_total%s %d %d\n", labels, c.ReadOps, ms)
		}
		if c.WriteOpsValid {
			fmt.Fprintf(&b, "ha_container_io_write_operations_total%s %d %d\n", labels, c.WriteOps, ms)
		}
	}
	success := 0
	if ok {
		success = 1
	}
	fmt.Fprintf(&b, "ha_container_io_collection_success %d %d\n", success, ms)
	if ok || s.Discovered > 0 {
		fmt.Fprintf(&b, "ha_container_io_discovered %d %d\n", s.Discovered, ms)
		fmt.Fprintf(&b, "ha_container_io_accounted %d %d\n", len(s.Containers), ms)
		fmt.Fprintf(&b, "ha_container_io_named %d %d\n", named, ms)
	}
	return b.Bytes()
}

var promEscapes = strings.NewReplacer(`\`, `\\`, `"`, `\"`, "\n", `\n`)

func promQuote(s string) string {
	return `"` + promEscapes.Replace(s) + `"`
}

func upload(ctx context.Context, client *http.Client, endpoint string, batch [][]byte) error {
	body := bytes.Join(batch, nil)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("invalid import request")
	}
	req.Header.Set("Content-Type", "text/plain; charset=utf-8")
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("metrics destination unreachable")
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return fmt.Errorf("metrics destination returned HTTP %d", resp.StatusCode)
	}
	return nil
}

func run(ctx context.Context, endpoint string) {
	client := &http.Client{Timeout: 8 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	tick := time.NewTicker(interval)
	defer tick.Stop()
	var src *source
	defer func() {
		if src != nil {
			src.close()
		}
	}()
	var pending [][]byte
	var sourceProblem, sinkProblem bool
	first := true
	dropped := uint64(0)
	for ctx.Err() == nil {
		var err error
		if src == nil {
			src, err = newSource()
		}
		var snap ioSnapshot
		if err == nil {
			call, cancel := context.WithTimeout(ctx, 15*time.Second)
			snap, err = src.snapshot(call)
			cancel()
		}
		at := time.Now()
		if err != nil {
			if !sourceProblem {
				log.Printf("Cannot sample container I/O: %v", err)
			}
		} else if sourceProblem || first {
			log.Printf("Read I/O counters for %d of %d container scopes", len(snap.Containers), snap.Discovered)
		}
		sourceProblem = err != nil
		first = false
		if len(pending) == pendingLimit {
			pending = pending[1:]
			dropped++
		}
		sample := render(snap, at, !sourceProblem)
		sample = fmt.Appendf(sample, "ha_container_io_dropped_samples_total %d %d\n", dropped, at.UnixMilli())
		pending = append(pending, sample)
		if err := upload(ctx, client, endpoint, pending); err != nil {
			if !sinkProblem {
				log.Printf("Buffering samples: %v", err)
			}
			sinkProblem = true
		} else {
			if sinkProblem {
				log.Printf("Delivered %d buffered samples", len(pending))
			}
			sinkProblem = false
			pending = nil
		}
		select {
		case <-tick.C:
		case <-ctx.Done():
			return
		}
	}
}

func main() {
	cfg, err := readOptions(os.Stdin)
	if err != nil {
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	run(ctx, cfg.MetricsURL)
}
