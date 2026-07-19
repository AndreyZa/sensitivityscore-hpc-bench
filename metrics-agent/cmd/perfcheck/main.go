// Command perfcheck is a minimal, standalone smoke test for whether
// cgroup-scoped perf_event_open() PMU counters on a given node not only
// OPEN but actually COUNT honestly — BEFORE building/deploying the full
// metrics-agent DaemonSet + Redis pipeline.
//
// Why this matters: for a long time cgroup-scoped opens failed with EINVAL
// "everywhere" and it was wrongly blamed on the hypervisor. That EINVAL was
// a bug in OUR code (cgroup events are per-CPU and need cpu>=0; we passed
// cpu=-1) — now fixed. So perfcheck no longer tests our code; it tests the
// honesty of the specific hardware/hypervisor, which genuinely varies:
//   - bare metal and Timeweb-cloud (KVM): open OK, reads real non-zero counts;
//   - VMware Workstation Pro: open OK, but reads exactly 0 — its vPMU fakes
//     syscall success without backing cgroup-scoped cache counting;
//   - hardened kernels / missing CAP_PERFMON: open FAILS (permissions).
//
// Nodes on one stand can differ, so run per node. Better to find out with a
// 10-line program than after wiring the whole agent/Redis/plugin chain.
//
// Usage: run as a pod with CAP_PERFMON, scoped to its own cgroup (self)
// rather than a specific pod's cgroup — that part of the real agent
// (cgroup path resolution) isn't being tested here, only whether the
// underlying syscall opens AND counts.
package main

import (
	"fmt"
	"os"
	"time"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/metrics-agent/pkg/perf"
)

func main() {
	fmt.Println("[perfcheck] opening self cgroup...")
	cgroupFile, err := perf.OpenPodCgroup("/sys/fs/cgroup")
	if err != nil {
		fmt.Printf("[perfcheck] FAILED to open cgroup: %v\n", err)
		os.Exit(1)
	}
	defer cgroupFile.Close()

	fmt.Println("[perfcheck] attempting to open PERF_COUNT_HW_CACHE_MISSES...")
	counter, err := perf.LLCMissesCounter(int(cgroupFile.Fd()))
	if err != nil {
		fmt.Printf("[perfcheck] FAILED to open perf counter: %v\n", err)
		fmt.Println("[perfcheck] cgroup-scoped perf_event_open() could not open here —")
		fmt.Println("[perfcheck] most likely permissions: missing CAP_PERFMON, or perf_event_paranoid")
		fmt.Println("[perfcheck] too high / a hardened-kernel policy. (EINVAL from the old cpu=-1 bug is fixed.)")
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

	reading, err := counter.ReadFull()
	if err != nil {
		fmt.Printf("[perfcheck] FAILED to read counter: %v\n", err)
		os.Exit(1)
	}
	misses := reading.Value

	fmt.Printf("[perfcheck] SUCCESS: PERF_COUNT_HW_CACHE_MISSES = %d\n", misses)

	// Мультиплексирование: сколько окна счётчик реально простоял на PMU. Здесь
	// открыт ровно один счётчик, поэтому в норме ratio = 1.000; заметно меньше
	// единицы на пустом узле означает, что PMU уже занята кем-то ещё (другой
	// профайлер, соседняя ВМ через vPMU) — на такой машине измерения кэш-оси
	// придётся оговаривать. Настоящая проверка — под нагрузкой стенда, где
	// событий открыто по числу подов (алерт SSPMUMultiplexed).
	ratio := reading.MultiplexRatio()
	fmt.Printf("[perfcheck] PMU multiplex ratio = %.3f (enabled=%d ns, running=%d ns)\n",
		ratio, reading.Enabled, reading.Running)
	if ratio < 0.999 {
		fmt.Println("[perfcheck] NOTE: counter was time-sliced even with a single event open —")
		fmt.Println("[perfcheck] raw counts get scaled by enabled/running, so cache-axis numbers")
		fmt.Println("[perfcheck] on this host rest on extrapolation, not direct measurement.")
	}
	if misses == 0 {
		fmt.Println("[perfcheck] WARNING: reading is exactly zero even after real memory activity —")
		fmt.Println("[perfcheck] this can mean the counter is opened but not actually backed by real")
		fmt.Println("[perfcheck] hardware (common when a hypervisor fakes the syscall success without")
		fmt.Println("[perfcheck] real PMU passthrough). Treat this as inconclusive, not a pass.")
		os.Exit(2)
	}

	fmt.Println("[perfcheck] PMU counters appear genuinely functional in this environment.")
}
