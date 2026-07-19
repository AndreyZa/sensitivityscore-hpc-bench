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
	"runtime"
	"strconv"
	"strings"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

// Counter is a set of cgroup-scoped hardware-counter fds — one per online CPU.
//
// cgroup-scoped perf events (PERF_FLAG_PID_CGROUP) are inherently per-CPU:
// perf_event_open(2) rejects cpu == -1 for them with EINVAL, regardless of
// privileges or perf_event_paranoid (a cgroup runs across many CPUs, so the
// kernel measures it one CPU at a time — exactly why `perf stat --cgroup`
// opens one event per CPU internally). Passing cpu == -1 here was a latent
// bug that made EVERY cgroup-scoped open fail with EINVAL, misread for years
// as "the hypervisor blocks PMU" — it was the argument, not the environment.
// Read() sums across all CPUs to give the cgroup's total.
type Counter struct {
	fds  []int
	name string
}

// onlineCPUs returns the kernel's online-CPU numbers from
// /sys/devices/system/cpu/online (e.g. "0-15" or "0,2-4,7"). A cgroup event
// must target an online CPU. Falls back to 0..NumCPU-1 if the file is
// unreadable (extremely rare — sysfs not mounted).
//
// The list is read once per Counter open, not tracked live: if a CPU is
// hot-unplugged later, reads on its fd return EOF -> the sampler errors out,
// is dropped, and the next tick's re-open picks up the new topology. Cloud
// VMs and the target stands don't hotplug CPUs, so a churn loop here is
// accepted rather than coded around.
func onlineCPUs() ([]int, error) {
	data, err := os.ReadFile("/sys/devices/system/cpu/online")
	if err != nil {
		n := runtime.NumCPU()
		cpus := make([]int, n)
		for i := range cpus {
			cpus[i] = i
		}
		return cpus, nil
	}
	return parseCPUList(strings.TrimSpace(string(data)))
}

// parseCPUList parses a Linux CPU-range list ("0-3,5,7-8") into explicit CPU
// numbers.
func parseCPUList(s string) ([]int, error) {
	var cpus []int
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		lo, hi, isRange := strings.Cut(part, "-")
		start, err := strconv.Atoi(lo)
		if err != nil {
			return nil, fmt.Errorf("parse cpu list %q: %w", s, err)
		}
		end := start
		if isRange {
			if end, err = strconv.Atoi(hi); err != nil {
				return nil, fmt.Errorf("parse cpu list %q: %w", s, err)
			}
		}
		for c := start; c <= end; c++ {
			cpus = append(cpus, c)
		}
	}
	if len(cpus) == 0 {
		return nil, fmt.Errorf("no CPUs parsed from %q", s)
	}
	return cpus, nil
}

// OpenCgroupCounter opens a cgroup-scoped hardware counter — one perf fd per
// online CPU (see Counter). cgroupFD must be an open file descriptor on the
// target pod's cgroup directory (e.g.
// /sys/fs/cgroup/kubepods.slice/.../<pod-cgroup>), obtained via os.Open(path).Fd().
//
// Requires CAP_PERFMON (see metrics-agent/deploy/daemonset.yaml) — deliberately
// not full `privileged: true`, see docs §3.1 for the rationale (this is part of
// the dissertation's argument that the approach is safer than a blanket
// privileged DaemonSet).
func OpenCgroupCounter(cgroupFD int, name string, typ uint32, config uint64) (*Counter, error) {
	cpus, err := onlineCPUs()
	if err != nil {
		return nil, fmt.Errorf("enumerate online CPUs for %s: %w", name, err)
	}

	c := &Counter{name: name, fds: make([]int, 0, len(cpus))}
	for _, cpu := range cpus {
		attr := unix.PerfEventAttr{
			Type:        typ,
			Config:      config,
			Size:        uint32(unsafe.Sizeof(unix.PerfEventAttr{})),
			Bits:        unix.PerfBitDisabled | unix.PerfBitInherit,
			Read_format: readFormat,
		}
		// cpu MUST be >= 0 for PERF_FLAG_PID_CGROUP (cgroup events are per-CPU;
		// cpu == -1 => EINVAL). groupFD=-1 (standalone). The first arg is the
		// cgroup fd, not a PID, in this mode.
		fd, err := unix.PerfEventOpen(&attr, cgroupFD, cpu, -1, unix.PERF_FLAG_PID_CGROUP)
		if err != nil {
			c.Close()
			return nil, fmt.Errorf("perf_event_open(%s) cpu=%d: %w", name, cpu, err)
		}
		c.fds = append(c.fds, fd)
	}
	return c, nil
}

// eventSpec identifies one hardware event for the pair opener below.
type eventSpec struct {
	name   string
	typ    uint32
	config uint64
}

// OpenCgroupCounterPair opens a numerator/denominator pair as ONE perf group
// per CPU: the numerator is the group leader, the denominator a member opened
// with groupFD = the leader's fd for that same CPU. The kernel then schedules
// both onto the PMU atomically together — under counter multiplexing (many
// monitored pods × few hardware counters, the normal state on a busy node)
// ungrouped events land in DIFFERENT time slices, and a ratio of two events
// measured over different windows is biased, not just noisy. Grouping is how
// `perf stat -e a,b` keeps ratios honest, and the pair costs the same two
// counters as before.
//
// Leaders are created disabled (Counter.Enable() on the numerator starts each
// group); members are created enabled so they simply follow their leader's
// schedule — calling Enable() on the denominator too is a harmless no-op.
func OpenCgroupCounterPair(cgroupFD int, num, den eventSpec) (*Counter, *Counter, error) {
	cpus, err := onlineCPUs()
	if err != nil {
		return nil, nil, fmt.Errorf("enumerate online CPUs for %s/%s: %w", num.name, den.name, err)
	}

	numC := &Counter{name: num.name, fds: make([]int, 0, len(cpus))}
	denC := &Counter{name: den.name, fds: make([]int, 0, len(cpus))}
	fail := func(cpu int, which string, err error) (*Counter, *Counter, error) {
		numC.Close()
		denC.Close()
		return nil, nil, fmt.Errorf("perf_event_open(%s) cpu=%d: %w", which, cpu, err)
	}
	for _, cpu := range cpus {
		leaderAttr := unix.PerfEventAttr{
			Type:        num.typ,
			Config:      num.config,
			Size:        uint32(unsafe.Sizeof(unix.PerfEventAttr{})),
			Bits:        unix.PerfBitDisabled | unix.PerfBitInherit,
			Read_format: readFormat,
		}
		leaderFD, err := unix.PerfEventOpen(&leaderAttr, cgroupFD, cpu, -1, unix.PERF_FLAG_PID_CGROUP)
		if err != nil {
			return fail(cpu, num.name, err)
		}
		numC.fds = append(numC.fds, leaderFD)

		memberAttr := unix.PerfEventAttr{
			Type:        den.typ,
			Config:      den.config,
			Size:        uint32(unsafe.Sizeof(unix.PerfEventAttr{})),
			Bits:        unix.PerfBitInherit, // NOT disabled: follows the leader
			Read_format: readFormat,
		}
		memberFD, err := unix.PerfEventOpen(&memberAttr, cgroupFD, cpu, leaderFD, unix.PERF_FLAG_PID_CGROUP)
		if err != nil {
			return fail(cpu, den.name, err)
		}
		denC.fds = append(denC.fds, memberFD)
	}
	return numC, denC, nil
}

// LLCPair opens llc_misses (leader) + llc_references (member) as one group per
// CPU — the honest way to measure llc_miss_rate = misses / references under
// multiplexing (see OpenCgroupCounterPair).
func LLCPair(cgroupFD int) (num, den *Counter, err error) {
	return OpenCgroupCounterPair(cgroupFD,
		eventSpec{"llc_misses", unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CACHE_MISSES},
		eventSpec{"llc_references", unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CACHE_REFERENCES},
	)
}

// NodePair opens node_load_misses (leader) + node_loads (member) as one group
// per CPU — numerator/denominator of numa_remote_ratio (see NodeLoadsCounter /
// NodeLoadMissesCounter for the event semantics).
func NodePair(cgroupFD int) (num, den *Counter, err error) {
	nodeMiss := hwCacheConfig(unix.PERF_COUNT_HW_CACHE_NODE,
		unix.PERF_COUNT_HW_CACHE_OP_READ, unix.PERF_COUNT_HW_CACHE_RESULT_MISS)
	nodeLoad := hwCacheConfig(unix.PERF_COUNT_HW_CACHE_NODE,
		unix.PERF_COUNT_HW_CACHE_OP_READ, unix.PERF_COUNT_HW_CACHE_RESULT_ACCESS)
	return OpenCgroupCounterPair(cgroupFD,
		eventSpec{"node_load_misses", unix.PERF_TYPE_HW_CACHE, nodeMiss},
		eventSpec{"node_loads", unix.PERF_TYPE_HW_CACHE, nodeLoad},
	)
}

// LLCMissesCounter opens PERF_COUNT_HW_CACHE_MISSES (LL cache misses), per the
// counter table in docs §3.1. Standalone (ungrouped) — used by the startup
// probe; the sampler's ratio path uses LLCPair.
func LLCMissesCounter(cgroupFD int) (*Counter, error) {
	return OpenCgroupCounter(cgroupFD, "llc_misses",
		unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CACHE_MISSES)
}

// LLCReferencesCounter opens PERF_COUNT_HW_CACHE_REFERENCES — kept for
// completeness/manual checks; the sampler uses LLCPair.
func LLCReferencesCounter(cgroupFD int) (*Counter, error) {
	return OpenCgroupCounter(cgroupFD, "llc_references",
		unix.PERF_TYPE_HARDWARE, unix.PERF_COUNT_HW_CACHE_REFERENCES)
}

// hwCacheConfig builds a PERF_TYPE_HW_CACHE event config per
// perf_event_open(2): cache id | (op << 8) | (result << 16).
func hwCacheConfig(cache, op, result uint64) uint64 {
	return cache | op<<8 | result<<16
}

// NodeLoadsCounter opens the generic node-level cache event ("node-loads" in
// perf-list terms): DRAM reads attributed to a NUMA node, access = local +
// remote. Denominator for numa_remote_ratio.
func NodeLoadsCounter(cgroupFD int) (*Counter, error) {
	return OpenCgroupCounter(cgroupFD, "node_loads",
		unix.PERF_TYPE_HW_CACHE,
		hwCacheConfig(unix.PERF_COUNT_HW_CACHE_NODE,
			unix.PERF_COUNT_HW_CACHE_OP_READ,
			unix.PERF_COUNT_HW_CACHE_RESULT_ACCESS))
}

// NodeLoadMissesCounter opens "node-load-misses": DRAM reads that missed the
// local NUMA node, i.e. were served by remote memory. Numerator for
// numa_remote_ratio = node-load-misses / node-loads.
//
// Unlike the uncore-IMC approach sketched in ReadUncoreNUMABandwidth, these
// generic events need no CPU-model-specific event codes — the kernel maps
// them per model, and models without a mapping fail the open with an explicit
// error instead of silently returning zeros.
func NodeLoadMissesCounter(cgroupFD int) (*Counter, error) {
	return OpenCgroupCounter(cgroupFD, "node_load_misses",
		unix.PERF_TYPE_HW_CACHE,
		hwCacheConfig(unix.PERF_COUNT_HW_CACHE_NODE,
			unix.PERF_COUNT_HW_CACHE_OP_READ,
			unix.PERF_COUNT_HW_CACHE_RESULT_MISS))
}

// readFormat is set on every event this package opens: the kernel then returns
// three u64 instead of one — value, time_enabled, time_running.
//
// Why it is not optional. When more events are open than the PMU has physical
// counters, the kernel time-slices them (multiplexing) but still returns the
// RAW count — as if the event had been on the PMU for the whole window. On a
// bench node the number of open events is proportional to the number of pods,
// i.e. to our experimental variable: the denser the node, the more the counter
// under-reports, so llc_misses_per_sec can FALL as load grows — the signal
// inverts exactly in the regime H1 tests. Without these two fields there is no
// way to tell that from data, only to suspect it.
const readFormat = unix.PERF_FORMAT_TOTAL_TIME_ENABLED | unix.PERF_FORMAT_TOTAL_TIME_RUNNING

// readSize is the byte size of the read(2) result under readFormat: three u64.
const readSize = 24

// Reading is one counter read: the value the kernel returned plus how long the
// event was scheduled. Enabled is the window the event was supposed to count,
// Running the part of it the event actually spent on the PMU; Enabled ==
// Running means no multiplexing.
type Reading struct {
	Value   uint64
	Enabled uint64
	Running uint64
}

// Scaled is the value corrected for multiplexing: value * enabled/running, the
// scaling perf(1) itself applies. Running == 0 (event never got onto the PMU
// in this window) yields 0 — the honest answer, since nothing was measured.
func (r Reading) Scaled() uint64 {
	if r.Running == 0 {
		return 0
	}
	if r.Running >= r.Enabled {
		return r.Value
	}
	return uint64(float64(r.Value) * float64(r.Enabled) / float64(r.Running))
}

// parseReading decodes the three little-endian u64 of a readFormat read.
func parseReading(buf []byte) (Reading, error) {
	if len(buf) < readSize {
		return Reading{}, fmt.Errorf("short read (%d bytes, want %d)", len(buf), readSize)
	}
	u64 := func(off int) uint64 {
		var v uint64
		for i := off + 7; i >= off; i-- {
			v = v<<8 | uint64(buf[i])
		}
		return v
	}
	return Reading{Value: u64(0), Enabled: u64(8), Running: u64(16)}, nil
}

// ReadFull returns the counter summed across every per-CPU fd, with each fd's
// value scaled by its OWN enabled/running before summing — the fds are
// scheduled independently, so a single node-wide ratio applied afterwards
// would be wrong. Enabled/Running of the result are the sums, which is what
// the node-level multiplexing ratio is computed from.
func (c *Counter) ReadFull() (Reading, error) {
	var total Reading
	buf := make([]byte, readSize)
	for _, fd := range c.fds {
		n, err := unix.Read(fd, buf)
		if err != nil {
			return Reading{}, fmt.Errorf("read perf counter %s: %w", c.name, err)
		}
		r, err := parseReading(buf[:n])
		if err != nil {
			return Reading{}, fmt.Errorf("read perf counter %s: %w", c.name, err)
		}
		total.Value += r.Scaled()
		total.Enabled += r.Enabled
		total.Running += r.Running
	}
	return total, nil
}

// Read returns the current cumulative counter value, summed across every
// per-CPU fd and corrected for multiplexing — the cgroup's total activity
// regardless of which CPU it ran on.
func (c *Counter) Read() (uint64, error) {
	r, err := c.ReadFull()
	if err != nil {
		return 0, err
	}
	return r.Value, nil
}

// MultiplexRatio is running/enabled over the counter's fds: 1.0 = the event sat
// on the PMU the whole time, < 1 = the kernel time-sliced it and the raw counts
// were scaled up to compensate. Below ~0.9 the scaling is doing heavy lifting
// and the numbers deserve a caveat (see the SSPMUMultiplexed alert). Returns 1
// when nothing was measured yet — no evidence of multiplexing is not evidence
// of it.
func (r Reading) MultiplexRatio() float64 {
	if r.Enabled == 0 {
		return 1
	}
	ratio := float64(r.Running) / float64(r.Enabled)
	if ratio > 1 {
		return 1
	}
	return ratio
}

func (c *Counter) Enable() error  { return c.ioctlAll(unix.PERF_EVENT_IOC_ENABLE) }
func (c *Counter) Disable() error { return c.ioctlAll(unix.PERF_EVENT_IOC_DISABLE) }
func (c *Counter) Reset() error   { return c.ioctlAll(unix.PERF_EVENT_IOC_RESET) }

// ioctlAll applies a perf ioctl to every per-CPU fd, stopping at the first error.
func (c *Counter) ioctlAll(req uint) error {
	for _, fd := range c.fds {
		if err := unix.IoctlSetInt(fd, req, 0); err != nil {
			return err
		}
	}
	return nil
}

// Close releases every per-CPU fd; safe on a partially-opened Counter (used by
// OpenCgroupCounter's own error path).
func (c *Counter) Close() error {
	var firstErr error
	for _, fd := range c.fds {
		if err := unix.Close(fd); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	c.fds = nil
	return firstErr
}

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
// PERF_FLAG_PID_CGROUP-scoped LLC-misses counter against cgroupPath (now
// per-CPU, see OpenCgroupCounter) rather than a plain per-process open, so it
// exercises the exact path the samplers use.
//
// NOTE: this probe used to fail with EINVAL "on WSL2/Docker Desktop", which
// was blamed on the hypervisor — that was actually the cpu == -1 bug in
// OpenCgroupCounter (cgroup events require cpu >= 0). With that fixed the
// probe opens and counts on WSL2 (confirmed real cache-miss counts). A
// zero/failed read now reflects the actual environment, not this bug — e.g.
// VMware Workstation Pro opens fine but reads exactly zero (its vPMU doesn't
// seem to virtualize cgroup-scoped cache-event counting), which is exactly
// the "opened but zero" inconclusive case handled below.
//
// It also treats "opened fine but read exactly zero after real memory
// activity" as unavailable: a hypervisor can fake syscall success without
// real PMU passthrough, and a counter that never counts is as useless to the
// agent as one that fails to open.
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
// NOTE: the WORKING numa_remote_ratio implementation is NodeLoadsCounter /
// NodeLoadMissesCounter above (generic node-level cache events via
// NewNUMARemoteRatioSampler) — per-cgroup, portable, no model-specific codes.
// This uncore path remains only as a potential refinement: it measures true
// per-socket memory *bandwidth* (all traffic, not just read misses), but is
// node-wide rather than per-cgroup and needs per-model event discovery.
//
// Reference approach: read the per-socket "CAS_COUNT.RD"/"CAS_COUNT.WR" uncore
// events for the NUMA node the cgroup's CPUs are pinned to (cross-reference
// /sys/fs/cgroup/<pod>/cpuset.cpus against /sys/devices/system/node/node*/cpulist),
// and compute remote_ratio by comparing local-node vs. remote-node memory traffic.
func ReadUncoreNUMABandwidth(nodeName string) (remoteRatio float64, err error) {
	return 0, fmt.Errorf("ReadUncoreNUMABandwidth: not implemented — requires PMU-model-specific uncore event discovery on node %s, see docs §3.1", nodeName)
}
