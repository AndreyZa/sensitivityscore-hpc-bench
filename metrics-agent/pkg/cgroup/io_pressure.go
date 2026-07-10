package cgroup

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// IOPressureStat holds the "some" line of a cgroup v2 io.pressure file (PSI,
// kernel >= 4.20 with CONFIG_PSI): SomeTotalUS is the monotonically growing
// total time (microseconds) at least one task in the cgroup was stalled
// waiting on IO. The agent samples it twice and derives
//
//	io_pressure = delta(SomeTotalUS) / window_us  in [0,1]
//
// — the share of wall time the cgroup spent stalled on IO. This is the IO
// dimension of the PressureVector: kernel-normalized contention, comparable
// across devices and stands, unlike raw IOPS which would need a per-device
// "max IOPS" calibration constant to mean anything on a [0,1] scale.
type IOPressureStat struct {
	SomeTotalUS uint64
}

// ReadIOPressure parses <cgroupPath>/io.pressure. The file looks like:
//
//	some avg10=0.00 avg60=0.00 avg300=0.00 total=38598883
//	full avg10=0.00 avg60=0.00 avg300=0.00 total=36029041
//
// Only the "some" total is read — the avg* fields are kernel-side EMAs over
// fixed horizons; the delta of total over our own tick window is exact.
// A missing file (os.IsNotExist(err)) means PSI is disabled in this kernel —
// callers should treat that as "IO dimension unavailable", not as an error
// worth retrying every tick.
func ReadIOPressure(cgroupPath string) (IOPressureStat, error) {
	path := cgroupPath + "/io.pressure"
	f, err := os.Open(path)
	if err != nil {
		return IOPressureStat{}, err // keep os.IsNotExist detectable for callers
	}
	defer f.Close()

	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) == 0 || fields[0] != "some" {
			continue
		}
		for _, kv := range fields[1:] {
			raw, ok := strings.CutPrefix(kv, "total=")
			if !ok {
				continue
			}
			v, err := strconv.ParseUint(raw, 10, 64)
			if err != nil {
				return IOPressureStat{}, fmt.Errorf("parse %s: bad total %q", path, raw)
			}
			return IOPressureStat{SomeTotalUS: v}, nil
		}
	}
	if err := scanner.Err(); err != nil {
		return IOPressureStat{}, fmt.Errorf("scan %s: %w", path, err)
	}
	return IOPressureStat{}, fmt.Errorf("no \"some ... total=\" line in %s", path)
}
