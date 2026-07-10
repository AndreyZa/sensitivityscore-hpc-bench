package cgroup

import (
	"os"
	"path/filepath"
	"testing"
)

func writePressureFile(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "io.pressure"), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestReadIOPressure(t *testing.T) {
	dir := writePressureFile(t,
		"some avg10=1.23 avg60=0.45 avg300=0.00 total=38598883\n"+
			"full avg10=0.00 avg60=0.00 avg300=0.00 total=36029041\n")

	got, err := ReadIOPressure(dir)
	if err != nil {
		t.Fatalf("ReadIOPressure: %v", err)
	}
	if got.SomeTotalUS != 38598883 {
		t.Errorf("SomeTotalUS = %d, want 38598883", got.SomeTotalUS)
	}
}

func TestReadIOPressureMissingFile(t *testing.T) {
	_, err := ReadIOPressure(t.TempDir())
	if !os.IsNotExist(err) {
		// The agent relies on this to distinguish "PSI disabled in this
		// kernel" (warn once, dimension off) from a transient read error.
		t.Errorf("expected os.IsNotExist error, got %v", err)
	}
}

func TestReadIOPressureMalformed(t *testing.T) {
	dir := writePressureFile(t, "full avg10=0.00 total=1\n")
	if _, err := ReadIOPressure(dir); err == nil {
		t.Error("expected error for file without a \"some\" line")
	}
}
