package cgroup

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// NetStats holds the network-byte counters tracked per pod cgroup.
type NetStats struct {
	RxBytes uint64
	TxBytes uint64
}

// TotalBytes returns combined rx+tx — the agent calls ReadNetStats twice
// (delta over the sample interval) to get a rate rather than a cumulative
// count, mirroring IOStats.IOPS() in cmd/agent/main.go's sampling loop.
func (s NetStats) TotalBytes() uint64 { return s.RxBytes + s.TxBytes }

// ReadNetStats sums non-loopback interface byte counters from
// /proc/<pid>/net/dev for a representative process in the pod's cgroup.
//
// This needs no eBPF, despite the original plan (docs §3.1) assuming a
// cgroup_skb hook would be required: cgroup v2 has no per-cgroup network
// byte counter file (unlike io.stat for disk I/O), but all containers in one
// pod share a single network namespace (the sandbox/"pause" container's) —
// confirmed against two different containers in the same pod, both reporting
// identical eth0 counters — so /proc/<pid>/net/dev for ANY live process in
// the pod already gives pod-wide totals. The agent runs with hostPID: true,
// so /proc/<pid> for any host process is visible directly, no /host/proc
// prefix or extra mount needed; SYS_PTRACE (already granted, see
// daemonset.yaml) is what actually permits reading another process's
// /proc/<pid>/net/dev across network namespaces.
func ReadNetStats(podCgroupPath string) (NetStats, error) {
	pid, err := representativePID(podCgroupPath)
	if err != nil {
		return NetStats{}, err
	}
	return parseProcNetDev(fmt.Sprintf("/proc/%d/net/dev", pid))
}

// representativePID finds a live process belonging to the pod. Processes
// live in per-container leaf cgroups nested under the pod-level cgroup
// (podCgroupPath itself has no cgroup.procs entries once a container starts —
// cgroup v2 kernels migrate tasks to the leaf), so scan direct children for
// the first non-empty cgroup.procs and take its first PID.
func representativePID(podCgroupPath string) (int, error) {
	entries, err := os.ReadDir(podCgroupPath)
	if err != nil {
		return 0, fmt.Errorf("read cgroup dir %s: %w", podCgroupPath, err)
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(podCgroupPath, e.Name(), "cgroup.procs"))
		if err != nil {
			continue
		}
		fields := strings.Fields(string(data))
		if len(fields) == 0 {
			continue
		}
		pid, err := strconv.Atoi(fields[0])
		if err != nil {
			continue
		}
		return pid, nil
	}
	return 0, fmt.Errorf("no live process found under any child of %s", podCgroupPath)
}

// parseProcNetDev sums rx/tx bytes across all non-loopback interfaces in the
// /proc/<pid>/net/dev format:
//
//	Inter-|   Receive                                                |  Transmit
//	 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
//	    lo: 1234 ...
//	  eth0: 5678 ...
func parseProcNetDev(path string) (NetStats, error) {
	f, err := os.Open(path)
	if err != nil {
		return NetStats{}, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	var stats NetStats
	scanner := bufio.NewScanner(f)
	lineNo := 0
	for scanner.Scan() {
		lineNo++
		if lineNo <= 2 {
			continue // two fixed header lines
		}
		parts := strings.SplitN(scanner.Text(), ":", 2)
		if len(parts) != 2 {
			continue
		}
		if strings.TrimSpace(parts[0]) == "lo" {
			continue
		}
		fields := strings.Fields(parts[1])
		if len(fields) < 9 {
			continue
		}
		rx, rxErr := strconv.ParseUint(fields[0], 10, 64) // receive: bytes is field 0
		tx, txErr := strconv.ParseUint(fields[8], 10, 64) // transmit: bytes is field 8 (after 8 receive fields)
		if rxErr != nil || txErr != nil {
			continue
		}
		stats.RxBytes += rx
		stats.TxBytes += tx
	}
	if err := scanner.Err(); err != nil {
		return NetStats{}, fmt.Errorf("scan %s: %w", path, err)
	}
	return stats, nil
}
