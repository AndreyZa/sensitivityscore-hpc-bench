package perf

import (
	"reflect"
	"testing"
)

func TestParseCPUList(t *testing.T) {
	cases := []struct {
		in   string
		want []int
	}{
		{"0", []int{0}},
		{"0-3", []int{0, 1, 2, 3}},
		{"0-15", []int{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}},
		{"0,2-4,7", []int{0, 2, 3, 4, 7}},
		{"1-1", []int{1}},
		{" 0-1 , 3 ", []int{0, 1, 3}},
	}
	for _, c := range cases {
		got, err := parseCPUList(c.in)
		if err != nil {
			t.Errorf("parseCPUList(%q) unexpected error: %v", c.in, err)
			continue
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("parseCPUList(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

func TestParseCPUListErrors(t *testing.T) {
	for _, in := range []string{"", "abc", "0-x", "-"} {
		if _, err := parseCPUList(in); err == nil {
			t.Errorf("parseCPUList(%q): expected error, got nil", in)
		}
	}
}

// TestLLCPairRealHardware exercises the grouped pair against the real PMU on
// the root cgroup: open -> enable -> touch memory -> read. Skips (not fails)
// where cgroup-scoped perf is unavailable (no PMU / no privilege) — on dev
// boxes and CI containers this is an environment property, not a bug. On any
// machine where it runs, it proves the leader+member group opens per CPU and
// both sides accumulate over the same window.
func TestLLCPairRealHardware(t *testing.T) {
	f, err := OpenPodCgroup("/sys/fs/cgroup")
	if err != nil {
		t.Skipf("open root cgroup: %v", err)
	}
	defer f.Close()

	num, den, err := LLCPair(int(f.Fd()))
	if err != nil {
		t.Skipf("cgroup-scoped PMU unavailable here: %v", err)
	}
	defer num.Close()
	defer den.Close()

	if err := num.Enable(); err != nil {
		t.Fatalf("enable leader: %v", err)
	}
	if err := den.Enable(); err != nil { // no-op by design, must not error
		t.Fatalf("enable member: %v", err)
	}

	// Generate cache traffic well past any LLC size so both events move.
	buf := make([]byte, 64<<20)
	for round := 0; round < 4; round++ {
		for i := 0; i < len(buf); i += 64 {
			buf[i]++
		}
	}

	misses, err := num.Read()
	if err != nil {
		t.Fatalf("read misses: %v", err)
	}
	refs, err := den.Read()
	if err != nil {
		t.Fatalf("read references: %v", err)
	}
	t.Logf("llc pair over window: misses=%d references=%d", misses, refs)
	if refs == 0 {
		t.Fatal("references == 0 — group member never scheduled with its leader")
	}
	if misses > refs*2 {
		// Sanity, not physics: grossly inverted magnitudes would mean the
		// pair is reading disjoint windows (the exact bug grouping prevents).
		t.Fatalf("misses (%d) wildly exceed references (%d)", misses, refs)
	}
}
