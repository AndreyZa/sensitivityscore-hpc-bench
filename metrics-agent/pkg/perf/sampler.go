package perf

import (
	"fmt"
	"os"
)

// RatioSampler keeps a numerator/denominator pair of cgroup-scoped hardware
// counters open across sampling ticks, so each SampleRate() call measures
// num/den over the window since the previous call (the agent's tick
// interval). An open-enable-read-close cycle per tick would measure a window
// of microseconds — the counters barely accumulate anything and the ratio is
// noise (this was found before any real config-A data was collected, so no
// measurements needed re-doing).
//
// It also owns the cgroup directory's *os.File: perf_event_open(2) with
// PERF_FLAG_PID_CGROUP keeps using that fd, and holding only the raw integer
// lets the os.File finalizer close it under GC while the counters still
// reference it.
//
// Concrete instances (see constructors below):
//   - LLC miss ratio:       llc_misses / llc_references
//   - NUMA remote ratio:    node-load-misses / node-loads (generic perf
//     node-level cache events — remote-DRAM read share; no CPU-model-specific
//     uncore event codes needed, the kernel maps them per model, and CPUs
//     without a mapping fail the open visibly instead of lying)
type RatioSampler struct {
	cgroupFile *os.File
	num        *Counter
	den        *Counter

	lastNum uint64
	lastDen uint64
	primed  bool
}

// counterPair opens the two counters for one RatioSampler flavor against an
// already-open cgroup fd.
type counterPair func(cgroupFD int) (num, den *Counter, err error)

func newRatioSampler(cgroupPath string, open counterPair) (*RatioSampler, error) {
	f, err := os.Open(cgroupPath)
	if err != nil {
		return nil, fmt.Errorf("open cgroup path %s: %w", cgroupPath, err)
	}
	s := &RatioSampler{cgroupFile: f}

	if s.num, s.den, err = open(int(f.Fd())); err != nil {
		s.Close()
		return nil, err
	}
	if err := s.num.Enable(); err != nil {
		s.Close()
		return nil, fmt.Errorf("enable %s: %w", s.num.name, err)
	}
	if err := s.den.Enable(); err != nil {
		s.Close()
		return nil, fmt.Errorf("enable %s: %w", s.den.name, err)
	}
	return s, nil
}

// NewLLCMissRatioSampler samples llc_misses / llc_references for the pod
// cgroup at cgroupPath — the LLC dimension of the PressureVector (docs §3.1).
// The pair is opened as one perf group per CPU (LLCPair) so numerator and
// denominator are co-scheduled under multiplexing — see OpenCgroupCounterPair.
func NewLLCMissRatioSampler(cgroupPath string) (*RatioSampler, error) {
	return newRatioSampler(cgroupPath, LLCPair)
}

// NewNUMARemoteRatioSampler samples node-load-misses / node-loads — the share
// of DRAM reads served by a remote NUMA node, i.e. the NUMA dimension of the
// PressureVector. Generic PERF_TYPE_HW_CACHE(NODE) events; on CPUs where the
// kernel has no node-event mapping the constructor fails with the underlying
// perf_event_open error — callers should treat that as "NUMA dimension
// unavailable on this host" (warn once, keep 0), not retry per tick.
func NewNUMARemoteRatioSampler(cgroupPath string) (*RatioSampler, error) {
	return newRatioSampler(cgroupPath, NodePair)
}

// SampleDeltas returns the raw numerator/denominator deltas over the window
// since the previous call — the caller derives the ratio via Ratio(), and the
// node-level aggregation sums deltas across pods so its ratio is weighted by
// each pod's actual traffic (averaging per-pod ratios would let one idle pod
// dilute a hot one). ok=false on the first (baseline-only) call. On error the
// sampler should be Close()d and re-created by the caller — the counters may
// be in an undefined state (e.g. the pod's cgroup was torn down mid-read).
func (s *RatioSampler) SampleDeltas() (deltaNum, deltaDen uint64, ok bool, err error) {
	curNum, err := s.num.Read()
	if err != nil {
		return 0, 0, false, err
	}
	curDen, err := s.den.Read()
	if err != nil {
		return 0, 0, false, err
	}

	deltaNum = curNum - s.lastNum
	deltaDen = curDen - s.lastDen
	wasPrimed := s.primed
	s.lastNum, s.lastDen = curNum, curDen
	s.primed = true

	if !wasPrimed {
		return 0, 0, false, nil
	}
	return deltaNum, deltaDen, true, nil
}

// Ratio is the num/den convenience for one sampler's deltas. deltaDen == 0
// (no denominator events this window — fully idle pod) is a true 0/0 and
// reports zero pressure rather than NaN.
func Ratio(deltaNum, deltaDen uint64) float64 {
	if deltaDen == 0 {
		return 0
	}
	return float64(deltaNum) / float64(deltaDen)
}

// Close releases both counters and the cgroup fd. Safe to call on a
// partially-constructed sampler.
func (s *RatioSampler) Close() {
	if s.num != nil {
		s.num.Close()
		s.num = nil
	}
	if s.den != nil {
		s.den.Close()
		s.den = nil
	}
	if s.cgroupFile != nil {
		s.cgroupFile.Close()
		s.cgroupFile = nil
	}
}
