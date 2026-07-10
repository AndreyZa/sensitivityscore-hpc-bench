// Local-dev fallback for environments where ProbeHardwareCounters fails
// (WSL2/Docker Desktop: no vPMU exposed to the guest kernel, confirmed via
// EINVAL rather than EACCES). This exists ONLY so the Redis write/read
// pipeline mechanics (docs §3.2) can be exercised on a laptop before the
// partner bare-metal stand is available (docs §0). It has no relationship to
// real cache behavior and must never be read as dissertation data — callers
// are required to tag samples produced from it with
// Sample.Approximation = "synthetic-devbox", a value distinct from the
// legitimate config-B "host-side" approximation (docs §3.3) so analysis never
// conflates the two.
package perf

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// SyntheticEstimator derives a plausible-but-fake [0,1] "pressure" ratio from
// host-wide /proc/stat CPU deltas between successive calls. It is intentionally
// crude (host-wide, not per-cgroup) since its only job is to put non-constant
// numbers through the Redis pipeline, not to approximate LLC behavior.
type SyntheticEstimator struct {
	haveSample        bool
	prevIdle, prevTot uint64
}

// NextRatio returns a value in [0,1] tracking recent host CPU busy-ness. The
// first call always returns 0 (no prior sample to diff against).
func (s *SyntheticEstimator) NextRatio() (float64, error) {
	idle, total, err := readProcStatCPU()
	if err != nil {
		return 0, err
	}
	defer func() { s.prevIdle, s.prevTot, s.haveSample = idle, total, true }()

	if !s.haveSample || total <= s.prevTot {
		return 0, nil
	}

	idleDelta := float64(idle - s.prevIdle)
	totalDelta := float64(total - s.prevTot)
	busy := 1 - idleDelta/totalDelta
	switch {
	case busy < 0:
		return 0, nil
	case busy > 1:
		return 1, nil
	default:
		return busy, nil
	}
}

// readProcStatCPU parses the aggregate "cpu" line of /proc/stat, returning
// idle (idle+iowait) and total jiffies.
func readProcStatCPU() (idle, total uint64, err error) {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0, 0, fmt.Errorf("open /proc/stat: %w", err)
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	if !scanner.Scan() {
		return 0, 0, fmt.Errorf("read /proc/stat: empty")
	}
	fields := strings.Fields(scanner.Text())
	if len(fields) < 5 || fields[0] != "cpu" {
		return 0, 0, fmt.Errorf("unexpected /proc/stat format: %q", scanner.Text())
	}

	vals := make([]uint64, 0, len(fields)-1)
	for _, f := range fields[1:] {
		v, err := strconv.ParseUint(f, 10, 64)
		if err != nil {
			return 0, 0, fmt.Errorf("parse /proc/stat field %q: %w", f, err)
		}
		vals = append(vals, v)
		total += v
	}
	idle = vals[3]
	if len(vals) > 4 {
		idle += vals[4] // iowait
	}
	return idle, total, nil
}
