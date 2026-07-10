package cgroup

import (
	"os"
	"path/filepath"
	"testing"
)

func writeNetDevFile(t *testing.T, content string) string {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "net_dev")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestParseProcNetDev(t *testing.T) {
	path := writeNetDevFile(t,
		"Inter-|   Receive                                                |  Transmit\n"+
			" face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"+
			"    lo: 1492424   17067    0    0    0     0          0         0  1492424   17067    0    0    0     0       0          0\n"+
			"  eth0: 17305250  126331    0    0    0     0          0         0  7006741   69349    0    0    0     0       0          0\n")

	got, err := parseProcNetDev(path)
	if err != nil {
		t.Fatalf("parseProcNetDev: %v", err)
	}
	if got.RxBytes != 17305250 {
		t.Errorf("RxBytes = %d, want 17305250 (lo must be excluded)", got.RxBytes)
	}
	if got.TxBytes != 7006741 {
		t.Errorf("TxBytes = %d, want 7006741", got.TxBytes)
	}
}

func TestParseProcNetDevMultiInterface(t *testing.T) {
	path := writeNetDevFile(t,
		"Inter-|   Receive                                                |  Transmit\n"+
			" face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"+
			"    lo:     100       1    0    0    0     0          0         0      100       1    0    0    0     0       0          0\n"+
			"  eth0:    1000      10    0    0    0     0          0         0      200      20    0    0    0     0       0          0\n"+
			"  eth1:    2000      10    0    0    0     0          0         0      300      20    0    0    0     0       0          0\n")

	got, err := parseProcNetDev(path)
	if err != nil {
		t.Fatalf("parseProcNetDev: %v", err)
	}
	if got.RxBytes != 3000 {
		t.Errorf("RxBytes = %d, want 3000 (sum of eth0+eth1, lo excluded)", got.RxBytes)
	}
	if got.TxBytes != 500 {
		t.Errorf("TxBytes = %d, want 500", got.TxBytes)
	}
}

func TestParseProcNetDevMissingFile(t *testing.T) {
	if _, err := parseProcNetDev(filepath.Join(t.TempDir(), "does-not-exist")); err == nil {
		t.Error("expected error for missing file")
	}
}

func TestRepresentativePID(t *testing.T) {
	dir := t.TempDir()

	// Empty leaf (container not started yet) should be skipped in favor of
	// the one with a real PID.
	emptyLeaf := filepath.Join(dir, "cri-containerd-empty.scope")
	if err := os.Mkdir(emptyLeaf, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(emptyLeaf, "cgroup.procs"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}

	realLeaf := filepath.Join(dir, "cri-containerd-real.scope")
	if err := os.Mkdir(realLeaf, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(realLeaf, "cgroup.procs"), []byte("4242\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	pid, err := representativePID(dir)
	if err != nil {
		t.Fatalf("representativePID: %v", err)
	}
	if pid != 4242 {
		t.Errorf("pid = %d, want 4242", pid)
	}
}

func TestRepresentativePIDNoProcesses(t *testing.T) {
	dir := t.TempDir()
	leaf := filepath.Join(dir, "cri-containerd-empty.scope")
	if err := os.Mkdir(leaf, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(leaf, "cgroup.procs"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}

	if _, err := representativePID(dir); err == nil {
		t.Error("expected error when no leaf cgroup has a live process")
	}
}
