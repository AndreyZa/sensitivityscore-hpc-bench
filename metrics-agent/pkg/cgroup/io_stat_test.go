package cgroup

import (
	"os"
	"path/filepath"
	"testing"
)

func writeIOStatFile(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "io.stat"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestReadIOStatSumsDevices(t *testing.T) {
	dir := writeIOStatFile(t,
		"259:0 rbytes=123456 wbytes=654321 rios=12 wios=34 dbytes=0 dios=0\n"+
			"8:0 rbytes=100 wbytes=200 rios=8 wios=6\n")

	got, err := ReadIOStat(dir)
	if err != nil {
		t.Fatalf("ReadIOStat: %v", err)
	}
	want := IOStats{ReadBytes: 123556, WriteBytes: 654521, ReadIOs: 20, WriteIOs: 40}
	if got != want {
		t.Errorf("ReadIOStat = %+v, want %+v", got, want)
	}
	if got.IOPS() != 60 {
		t.Errorf("IOPS() = %d, want 60", got.IOPS())
	}
}

func TestReadIOStatEmptyFile(t *testing.T) {
	// A pod that has done no IO yet has an empty io.stat — that's zero
	// counters, not an error.
	got, err := ReadIOStat(writeIOStatFile(t, ""))
	if err != nil {
		t.Fatalf("ReadIOStat on empty file: %v", err)
	}
	if got != (IOStats{}) {
		t.Errorf("ReadIOStat = %+v, want zero IOStats", got)
	}
}

func TestReadIOStatMissingFile(t *testing.T) {
	if _, err := ReadIOStat(t.TempDir()); err == nil {
		t.Error("expected error for missing io.stat")
	}
}
