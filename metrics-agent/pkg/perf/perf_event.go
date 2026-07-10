// Package perf wraps the perf_event_open(2) syscall for the two PMU counters the
// dissertation treats as "honest" hardware measurements (docs §3.1): LLC misses and
// LLC references, scoped to a pod's cgroup (PERF_FLAG_PID_CGROUP, available since
// kernel 4.x). NUMA bandwidth requires raw uncore-PMU events that are CPU-model
// specific (Intel uncore_imc_*) — see ReadUncoreNUMABandwidth for the integration
// point and why it can't be a static const like the LLC counters.
package perf

import (
	"fmt"
	"os"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// Counter is an open perf_event_open() file descriptor scoped to one cgroup.
type Counter struct {
	fd   int
	name string
}

// OpenCgroupCounter opens a cgroup-scoped hardware counter. cgroupFD must be an
// open file descriptor on the target pod's cgroup directory (e.g.
// /sys/fs/cgroup/kubepods.slice/.../<pod-cgroup>), obtained via os.Open(path).Fd().
//
// Requires CAP_PERFMON (see metrics-agent/deploy/daemonset.yaml) — deliberately
// not full `privileged: true`, see docs §3.1 for the rationale (this is part of
// the dissertation's argument that the approach is safer than a blanket
// privileged DaemonSet).
func OpenCgroupCounter(cgroupFD int, name string, typ uint32, config uint64) (*Counter, error) {
	attr := unix.PerfEventAttr{
		Type:   typ,
		Config: config,
		Size:   uint32(unsafe.Sizeof(unix.PerfEventAttr{})),
		Bits:   unix.PerfBitDisabled | unix.PerfBitInherit,
	}

	// cpu=-1 (any CPU), groupFD=-1 (standalone counter), flags=PID_CGROUP because
	// the first arg is interpreted as a cgroup fd rather than a PID in that mode.
	fd, err := unix.PerfEventOpen(&attr, cgroupFD, -1, -1, unix.PERF_FLAG_PID_CGROUP)
	if err != nil {
		return nil, fmt.Errorf("perf_event_open(%s): %w", name, err)
	}
	return &Counter{fd: fd, name: name}, nil
}

// LLCMissesCounter opens PERF_COUNT_HW_CACHE_MISSES (LL cache misses), per the
// counter table in docs §3.1.
func LLCMissesCounter(cgroupFD int) (*Counter, error) {
	return OpenCgroupCounter(cgroupFD, "llc_misses",
		unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CACHE_MISSES)
}

// LLCReferencesCounter opens PERF_COUNT_HW_CACHE_REFERENCES — used as the
// normalization denominator for llc_miss_rate = misses / references.
func LLCReferencesCounter(cgroupFD int) (*Counter, error) {
	return OpenCgroupCounter(cgroupFD, "llc_references",
		unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CACHE_REFERENCES)
}

// Read returns the current cumulative counter value.
func (c *Counter) Read() (uint64, error) {
	buf := make([]byte, 8)
	n, err := unix.Read(c.fd, buf)
	if err != nil {
		return 0, fmt.Errorf("read perf counter %s: %w", c.name, err)
	}
	if n != 8 {
		return 0, fmt.Errorf("read perf counter %s: short read (%d bytes)", c.name, n)
	}
	var v uint64
	for i := 7; i >= 0; i-- {
		v = v<<8 | uint64(buf[i])
	}
	return v, nil
}

func (c *Counter) Enable() error  { return unix.IoctlSetInt(c.fd, unix.PERF_EVENT_IOC_ENABLE, 0) }
func (c *Counter) Disable() error { return unix.IoctlSetInt(c.fd, unix.PERF_EVENT_IOC_DISABLE, 0) }
func (c *Counter) Reset() error   { return unix.IoctlSetInt(c.fd, unix.PERF_EVENT_IOC_RESET, 0) }
func (c *Counter) Close() error   { return unix.Close(c.fd) }

// OpenPodCgroup opens the cgroup directory for a given cgroup v2 path; pass
// int(f.Fd()) into OpenCgroupCounter. Path resolution (pod UID -> cgroup path
// under /sys/fs/cgroup/kubepods.slice/...) is the caller's responsibility — it
// differs between cgroup driver configs (systemd vs cgroupfs), see cgroup package.
//
// Returns the *os.File rather than a raw fd: the caller must keep the File
// alive (and Close it) for as long as any counter opened against it exists —
// returning only the int let the os.File finalizer close the fd under GC
// while counters still referenced it.
func OpenPodCgroup(cgroupPath string) (*os.File, error) {
	f, err := os.Open(cgroupPath)
	if err != nil {
		return nil, fmt.Errorf("open cgroup path %s: %w", cgroupPath, err)
	}
	return f, nil
}

// ProbeHardwareCounters mirrors cmd/perfcheck's check — opening an actual
// PERF_FLAG_PID_CGROUP-scoped LLC-misses counter against cgroupPath — rather
// than a plain per-process open. A plain, non-cgroup-scoped open silently
// succeeds on WSL2/Docker Desktop even though the cgroup-scoped mode
// sampleCgroup actually depends on fails there with EINVAL, so testing
// anything less than the real code path gives a false positive.
//
// It also treats "opened fine but read exactly zero after real memory
// activity" as unavailable: cmd/perfcheck documents this as the hypervisor
// faking syscall success without real PMU passthrough, and a counter that
// never counts is as useless to the agent as one that fails to open.
func ProbeHardwareCounters(cgroupPath string) (bool, error) {
	cgroupFile, err := OpenPodCgroup(cgroupPath)
	if err != nil {
		return false, fmt.Errorf("open cgroup %s: %w", cgroupPath, err)
	}
	defer cgroupFile.Close()
	counter, err := LLCMissesCounter(int(cgroupFile.Fd()))
	if err != nil {
		return false, err
	}
	defer counter.Close()

	if err := counter.Enable(); err != nil {
		return false, fmt.Errorf("enable probe counter: %w", err)
	}

	deadline := time.Now().Add(200 * time.Millisecond)
	buf := make([]byte, 64*1024*1024) // forces real cache traffic, see cmd/perfcheck/main.go
	for time.Now().Before(deadline) {
		for i := range buf {
			buf[i]++
		}
	}

	misses, err := counter.Read()
	if err != nil {
		return false, fmt.Errorf("read probe counter: %w", err)
	}
	if misses == 0 {
		return false, fmt.Errorf("counter opened but read exactly zero after real memory activity (likely a hypervisor faking the syscall — see cmd/perfcheck)")
	}
	return true, nil
}

// ReadUncoreNUMABandwidth is the integration point for NUMA-bandwidth metrics via
// Intel uncore IMC (integrated memory controller) counters, exposed under
// /sys/bus/event_source/devices/uncore_imc_*/ (docs §3.1). This requires querying
// the host's PMU model (CPUID) to pick the right uncore_imc_N device and event
// code — it is NOT a generic perf_event_open() call like the LLC counters above,
// so it's intentionally left as an explicit TODO with the architecture documented
// rather than a guessed/untested raw-event encoding.
//
// Reference approach: read the per-socket "CAS_COUNT.RD"/"CAS_COUNT.WR" uncore
// events for the NUMA node the cgroup's CPUs are pinned to (cross-reference
// /sys/fs/cgroup/<pod>/cpuset.cpus against /sys/devices/system/node/node*/cpulist),
// and compute remote_ratio by comparing local-node vs. remote-node memory traffic.
func ReadUncoreNUMABandwidth(nodeName string) (remoteRatio float64, err error) {
	return 0, fmt.Errorf("ReadUncoreNUMABandwidth: not implemented — requires PMU-model-specific uncore event discovery on node %s, see docs §3.1", nodeName)
}
