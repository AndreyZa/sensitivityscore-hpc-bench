package perf

import (
	"fmt"
	"os"
)

// CgroupLLCSampler keeps the two LLC counters for one pod cgroup open across
// sampling ticks, so each SampleRate() call measures misses/references over
// the window since the previous call (the agent's tick interval). The
// previous open-enable-read-close-per-tick cycle measured a window of
// microseconds — the counters barely accumulated anything and the resulting
// ratio was noise, not a рабочая метрика (this was found before any real
// config-A data was collected, so no measurements need re-doing).
//
// It also owns the cgroup directory's *os.File: perf_event_open(2) with
// PERF_FLAG_PID_CGROUP keeps using that fd, and holding only the raw integer
// (as the old code did) lets the os.File finalizer close it under GC while
// the counters still reference it.
type CgroupLLCSampler struct {
	cgroupFile *os.File
	misses     *Counter
	refs       *Counter

	lastMisses uint64
	lastRefs   uint64
	primed     bool
}

// NewCgroupLLCSampler opens and enables both LLC counters against cgroupPath.
// The first SampleRate() call only records the baseline (ok=false); every
// call after that returns the rate over the elapsed window.
func NewCgroupLLCSampler(cgroupPath string) (*CgroupLLCSampler, error) {
	f, err := os.Open(cgroupPath)
	if err != nil {
		return nil, fmt.Errorf("open cgroup path %s: %w", cgroupPath, err)
	}
	s := &CgroupLLCSampler{cgroupFile: f}

	if s.misses, err = LLCMissesCounter(int(f.Fd())); err != nil {
		s.Close()
		return nil, err
	}
	if s.refs, err = LLCReferencesCounter(int(f.Fd())); err != nil {
		s.Close()
		return nil, err
	}
	if err := s.misses.Enable(); err != nil {
		s.Close()
		return nil, fmt.Errorf("enable llc_misses: %w", err)
	}
	if err := s.refs.Enable(); err != nil {
		s.Close()
		return nil, fmt.Errorf("enable llc_references: %w", err)
	}
	return s, nil
}

// SampleRate returns misses/references over the window since the previous
// call. ok=false on the first (baseline-only) call. On error the sampler
// should be Close()d and re-created by the caller — the counters may be in
// an undefined state (e.g. the pod's cgroup was torn down mid-read).
func (s *CgroupLLCSampler) SampleRate() (rate float64, ok bool, err error) {
	curMisses, err := s.misses.Read()
	if err != nil {
		return 0, false, err
	}
	curRefs, err := s.refs.Read()
	if err != nil {
		return 0, false, err
	}

	deltaMisses := curMisses - s.lastMisses
	deltaRefs := curRefs - s.lastRefs
	wasPrimed := s.primed
	s.lastMisses, s.lastRefs = curMisses, curRefs
	s.primed = true

	if !wasPrimed {
		return 0, false, nil
	}
	if deltaRefs == 0 {
		// No LLC traffic at all this window (fully idle pod) — a true 0/0;
		// report zero pressure rather than NaN.
		return 0, true, nil
	}
	return float64(deltaMisses) / float64(deltaRefs), true, nil
}

// Close releases both counters and the cgroup fd. Safe to call on a
// partially-constructed sampler.
func (s *CgroupLLCSampler) Close() {
	if s.misses != nil {
		s.misses.Close()
		s.misses = nil
	}
	if s.refs != nil {
		s.refs.Close()
		s.refs = nil
	}
	if s.cgroupFile != nil {
		s.cgroupFile.Close()
		s.cgroupFile = nil
	}
}
