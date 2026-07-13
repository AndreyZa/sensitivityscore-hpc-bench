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
