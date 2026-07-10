// Command perfcheck is a minimal, standalone smoke test for whether
// perf_event_open()-based PMU counters are actually accessible in the
// current environment — BEFORE building/deploying the full metrics-agent
// DaemonSet + Redis pipeline.
//
// Why this matters specifically for local Docker Desktop testing: Docker
// Desktop's Kubernetes runs inside a Linux VM (not on bare metal), and
// access to hardware performance counters from within a VM guest is
// frequently blocked or faked by the hypervisor unless PMU passthrough is
// explicitly enabled — this is the exact same concern already documented
// in docs/Технический_план_экспериментов.md §3.3 for KubeVirt VMs, just
// applying here to the Docker Desktop VM itself. Better to find out with a
// 10-line program than after wiring the whole agent/Redis/plugin chain.
//
// Usage: run as a pod with CAP_PERFMON, scoped to its own cgroup (self)
// rather than a specific pod's cgroup — that part of the real agent
// (cgroup path resolution) isn't being tested here, only the underlying
// syscall's availability.
package main

import (
	"fmt"
	"os"
	"time"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/metrics-agent/pkg/perf"
)

func main() {
	fmt.Println("[perfcheck] opening self cgroup...")
	cgroupFD, err := perf.OpenPodCgroup("/sys/fs/cgroup")
	if err != nil {
		fmt.Printf("[perfcheck] FAILED to open cgroup: %v\n", err)
		os.Exit(1)
	}

	fmt.Println("[perfcheck] attempting to open PERF_COUNT_HW_CACHE_MISSES...")
	counter, err := perf.LLCMissesCounter(cgroupFD)
	if err != nil {
		fmt.Printf("[perfcheck] FAILED to open perf counter: %v\n", err)
		fmt.Println("[perfcheck] this means perf_event_open() is not usable here —")
		fmt.Println("[perfcheck] likely blocked by the hypervisor (Docker Desktop's VM) or missing CAP_PERFMON.")
		os.Exit(1)
	}
	defer counter.Close()

	if err := counter.Enable(); err != nil {
		fmt.Printf("[perfcheck] FAILED to enable counter: %v\n", err)
		os.Exit(1)
	}

	// Busy-loop briefly so there's actually some cache activity to count —
	// a static zero reading wouldn't distinguish "works but idle" from
	// "silently returns zero because it's not really counting anything".
	fmt.Println("[perfcheck] generating some memory activity for ~1s...")
	deadline := time.Now().Add(1 * time.Second)
	buf := make([]byte, 64*1024*1024) // 64MB — larger than typical LLC, forces real cache traffic
	for time.Now().Before(deadline) {
		for i := range buf {
			buf[i]++
		}
	}

	misses, err := counter.Read()
	if err != nil {
		fmt.Printf("[perfcheck] FAILED to read counter: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("[perfcheck] SUCCESS: PERF_COUNT_HW_CACHE_MISSES = %d\n", misses)
	if misses == 0 {
		fmt.Println("[perfcheck] WARNING: reading is exactly zero even after real memory activity —")
		fmt.Println("[perfcheck] this can mean the counter is opened but not actually backed by real")
		fmt.Println("[perfcheck] hardware (common when a hypervisor fakes the syscall success without")
		fmt.Println("[perfcheck] real PMU passthrough). Treat this as inconclusive, not a pass.")
		os.Exit(2)
	}

	fmt.Println("[perfcheck] PMU counters appear genuinely functional in this environment.")
}
