//go:build linux

package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"syscall"
	"time"

	"github.com/coreos/go-systemd/v22/sdjournal"
	"github.com/godbus/dbus/v5"
)

type containerIO struct {
	ID, Name                    string
	Read, Written               uint64
	ReadOps, WriteOps           uint64
	ReadOpsValid, WriteOpsValid bool
}
type ioSnapshot struct {
	Containers []containerIO
	Discovered int
}
type nameEntry struct {
	name  string
	retry time.Time
}
type nameResult struct{ id, name string }
type source struct {
	bus       *dbus.Conn
	names     map[string]nameEntry
	requests  chan string
	resolved  chan nameResult
	stop      chan struct{}
	nextScope int
}
type journalReader struct{ journals map[string]*sdjournal.Journal }

const namesPerCycle = 4

type systemdUnit struct {
	Name, Description, LoadState, ActiveState, SubState, Following string
	Path                                                           dbus.ObjectPath
	JobID                                                          uint32
	JobType                                                        string
	JobPath                                                        dbus.ObjectPath
}

var scopeName = regexp.MustCompile(`^docker-([0-9a-f]{64})\.scope$`)

func newSource() (*source, error) {
	bus, err := dbus.ConnectSystemBus()
	if err != nil {
		return nil, fmt.Errorf("connect system bus: %w", err)
	}
	s := &source{bus: bus, names: make(map[string]nameEntry), requests: make(chan string, namesPerCycle), resolved: make(chan nameResult, 2*namesPerCycle), stop: make(chan struct{})}
	go s.lookupNames()
	return s, nil
}

func (s *source) close() {
	close(s.stop)
	s.bus.Close()
}

// Bookworm's libsystemd requires the same OS thread for the lifetime of a
// journal handle. Journal reads can block on the very disk we are measuring.
func (s *source) lookupNames() {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	j := journalReader{journals: make(map[string]*sdjournal.Journal)}
	defer func() {
		for _, handle := range j.journals {
			handle.Close()
		}
	}()
	for {
		select {
		case <-s.stop:
			return
		case id := <-s.requests:
			if err := j.refreshJournals(); err != nil {
				continue
			}
			name, err := j.nameFor(id)
			if err != nil || name == "" {
				continue
			}
			select {
			case s.resolved <- nameResult{id: id, name: name}:
			case <-s.stop:
				return
			}
		}
	}
}

// These paths must be read-only bind mounts of the HAOS journal roots.
var journalRoots = []string{"/var/log/journal", "/run/log/journal"}
var machineID = regexp.MustCompile(`^[0-9a-f]{32}$`)

func (s *journalReader) refreshJournals() error {
	seen := make(map[string]bool)
	for _, root := range journalRoots {
		entries, err := os.ReadDir(root)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return fmt.Errorf("read journal directory: %w", err)
		}
		for _, entry := range entries {
			if !entry.IsDir() || !machineID.MatchString(entry.Name()) {
				continue
			}
			path := filepath.Join(root, entry.Name())
			seen[path] = true
			if _, ok := s.journals[path]; !ok {
				j, err := sdjournal.NewJournalFromDir(path)
				if err != nil {
					return fmt.Errorf("open journal directory: %w", err)
				}
				s.journals[path] = j
			}
		}
	}
	for path, j := range s.journals {
		if !seen[path] {
			j.Close()
			delete(s.journals, path)
		}
	}
	if len(s.journals) == 0 {
		return fmt.Errorf("no host journal directories available")
	}
	return nil
}

func (s *journalReader) nameFor(id string) (string, error) {
	name := ""
	for _, root := range journalRoots {
		for path, j := range s.journals {
			if filepath.Dir(path) != root {
				continue
			}
			if rc := j.Wait(0); rc < 0 {
				return "", fmt.Errorf("refresh journal: %w", syscall.Errno(-rc))
			}
			j.FlushMatches()
			if err := j.AddMatch("CONTAINER_ID_FULL=" + id); err != nil {
				return "", fmt.Errorf("match journal: %w", err)
			}
			if err := j.SeekTail(); err != nil {
				return "", fmt.Errorf("seek journal: %w", err)
			}
			n, err := j.Previous()
			if err != nil {
				return "", fmt.Errorf("read journal: %w", err)
			}
			if n == 0 {
				continue
			}
			name, err = j.GetDataValue("CONTAINER_NAME")
			if errors.Is(err, syscall.ENOENT) || errors.Is(err, syscall.ENODATA) {
				name = ""
				continue
			}
			if err != nil {
				return "", fmt.Errorf("read journal field: %w", err)
			}
			if name != "" {
				break
			}
		}
		if name != "" {
			break
		}
	}

	return name, nil
}

func (s *source) snapshot(ctx context.Context) (ioSnapshot, error) {
	if !s.bus.Connected() {
		bus, err := dbus.ConnectSystemBus()
		if err != nil {
			return ioSnapshot{}, fmt.Errorf("reconnect system bus: %w", err)
		}
		s.bus.Close()
		s.bus = bus
	}
	var units []systemdUnit
	manager := s.bus.Object("org.freedesktop.systemd1", dbus.ObjectPath("/org/freedesktop/systemd1"))
	err := manager.CallWithContext(ctx, "org.freedesktop.systemd1.Manager.ListUnitsByPatterns", 0, []string{"active"}, []string{"docker-*.scope"}).Store(&units)
	if err != nil {
		return ioSnapshot{}, fmt.Errorf("list docker scopes: %w", err)
	}
	// A stable order lets the next pass resume after a timed-out scope.
	sort.Slice(units, func(i, j int) bool { return units[i].Name < units[j].Name })
	active := make(map[string]struct{}, len(units))
	scopes := units[:0]
	for _, unit := range units {
		if m := scopeName.FindStringSubmatch(unit.Name); m != nil {
			active[m[1]] = struct{}{}
			scopes = append(scopes, unit)
		}
	}
	result := ioSnapshot{Discovered: len(scopes)}
	var sampleErr error
	start := s.nextScope
	for i := range scopes {
		index := (start + i) % len(scopes)
		unit := scopes[index]
		id := unit.Name[len("docker-") : len(unit.Name)-len(".scope")]
		s.nextScope = (index + 1) % len(scopes)
		var props map[string]dbus.Variant
		obj := s.bus.Object("org.freedesktop.systemd1", unit.Path)
		if err := obj.CallWithContext(ctx, "org.freedesktop.DBus.Properties.GetAll", 0, "org.freedesktop.systemd1.Scope").Store(&props); err != nil {
			var de dbus.Error
			if errors.As(err, &de) && (de.Name == "org.freedesktop.DBus.Error.UnknownObject" || de.Name == "org.freedesktop.systemd1.NoSuchUnit") {
				continue
			}
			sampleErr = fmt.Errorf("get scope properties: %w", err)
			if ctx.Err() != nil || !s.bus.Connected() {
				break
			}
			continue
		}
		read, rok := props["IOReadBytes"].Value().(uint64)
		written, wok := props["IOWriteBytes"].Value().(uint64)
		if !rok || !wok || read == math.MaxUint64 || written == math.MaxUint64 {
			continue
		}
		readOps, rv := props["IOReadOperations"].Value().(uint64)
		writeOps, wv := props["IOWriteOperations"].Value().(uint64)
		result.Containers = append(result.Containers, containerIO{
			ID: id, Read: read, Written: written,
			ReadOps: readOps, ReadOpsValid: rv && readOps != math.MaxUint64,
			WriteOps: writeOps, WriteOpsValid: wv && writeOps != math.MaxUint64,
		})
	}
	s.applyNames(result.Containers, active, time.Now())
	return result, sampleErr
}

func (s *source) applyNames(rows []containerIO, active map[string]struct{}, now time.Time) {
	for len(s.resolved) > 0 {
		found := <-s.resolved
		if _, ok := active[found.id]; ok {
			s.names[found.id] = nameEntry{name: found.name}
		}
	}
	for id := range s.names {
		if _, ok := active[id]; !ok {
			delete(s.names, id)
		}
	}
	// New IDs are considered before retrying IDs for which no name was found.
	candidates := make([]string, 0)
	for i := range rows {
		entry := s.names[rows[i].ID]
		rows[i].Name = entry.name
		if entry.name == "" && !now.Before(entry.retry) {
			candidates = append(candidates, rows[i].ID)
		}
	}
	sort.SliceStable(candidates, func(i, j int) bool { return s.names[candidates[i]].retry.Before(s.names[candidates[j]].retry) })
	for i, id := range candidates {
		if i == namesPerCycle {
			break
		}
		select {
		case s.requests <- id:
			s.names[id] = nameEntry{retry: now.Add(5 * time.Minute)}
		default:
			return
		}
	}
}
