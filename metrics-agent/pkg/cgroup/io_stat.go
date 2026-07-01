// Package cgroup reads cgroup v2 statistics for the two S dimensions that are
// cheaper/more reliable to source from cgroup files than from perf_event_open()
// (docs §3.1): Disk I/O via io.stat (blkio v2), and a network byte-counter via
// the pod's net_cls classid (paired with an eBPF socket-hook collector, see
// net.go for why network can't be a plain cgroup-file read like io.stat).
package cgroup

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// IOStats holds the subset of cgroup v2 io.stat fields the dissertation tracks.
type IOStats struct {
	ReadBytes  uint64
	WriteBytes uint64
	ReadIOs    uint64
	WriteIOs   uint64
}

// ReadIOStat parses /sys/fs/cgroup/<...>/io.stat for a pod cgroup. The file has one
// line per backing device, e.g.:
//
//	259:0 rbytes=123456 wbytes=654321 rios=12 wios=34 dbytes=0 dios=0
//
// Multiple device lines (rare for a single-disk node, common on stends with
// separate data/scratch volumes) are summed.
func ReadIOStat(cgroupPath string) (IOStats, error) {
	path := cgroupPath + "/io.stat"
	f, err := os.Open(path)
	if err != nil {
		return IOStats{}, fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()

	var stats IOStats
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		for _, kv := range fields[1:] { // fields[0] is the "major:minor" device id
			parts := strings.SplitN(kv, "=", 2)
			if len(parts) != 2 {
				continue
			}
			v, err := strconv.ParseUint(parts[1], 10, 64)
			if err != nil {
				continue
			}
			switch parts[0] {
			case "rbytes":
				stats.ReadBytes += v
			case "wbytes":
				stats.WriteBytes += v
			case "rios":
				stats.ReadIOs += v
			case "wios":
				stats.WriteIOs += v
			}
		}
	}
	if err := scanner.Err(); err != nil {
		return IOStats{}, fmt.Errorf("scan %s: %w", path, err)
	}
	return stats, nil
}

// IOPS returns the combined read+write IOPS counter — the agent calls this twice
// (delta over the sample interval) to get a rate rather than a cumulative count,
// see cmd/agent/main.go's sampling loop.
func (s IOStats) IOPS() uint64 { return s.ReadIOs + s.WriteIOs }
