// Package vpmu implements the vPMU-availability health-check required before
// starting a config-B (KubeVirt) experiment series (docs §3.3): if vPMU
// passthrough is available, the agent should collect LLC/NUMA counters from
// inside the guest (honest data); if not, it falls back to host-side cgroup
// readings on the qemu-kvm process and the run must be tagged
// approximation="host-side" so analysis doesn't silently mix exact (A/C) and
// approximated (B) data.
package vpmu

import (
	"context"
	"fmt"
	"os"
	"os/exec"
)

// Status is the result of a single VMI's vPMU health-check, persisted into the
// run's metadata (docs §3.3: "фиксировать факт приближения в метаданных
// прогона").
type Status struct {
	VMIName       string
	Available     bool
	Approximation string // "" if Available, "host-side" otherwise
	Detail        string // human-readable reason, for the advisor briefing / logs
}

// CheckGuestPMU runs the in-guest check: presence of /sys/devices/cpu/... inside
// the VM (the standard sysfs PMU device tree) is treated as evidence vPMU is
// exposed to the guest. This requires guest-exec access (e.g. via
// `virtctl ssh`/qemu-guest-agent exec) — left here as the documented contract;
// wiring to the specific stand's guest-access mechanism (qemu-guest-agent vs.
// SSH) happens in cmd/agent/main.go.
func CheckGuestPMU(ctx context.Context, execInGuest func(ctx context.Context, cmd string) (string, error)) (bool, string, error) {
	out, err := execInGuest(ctx, "test -d /sys/devices/cpu && echo present || echo absent")
	if err != nil {
		return false, "", fmt.Errorf("guest exec failed: %w", err)
	}
	available := out == "present\n" || out == "present"
	return available, out, nil
}

// CheckHostCapabilities cross-checks via `virsh capabilities` on the host whether
// the hypervisor advertises vPMU support at all — a necessary but not sufficient
// condition (the VMI spec must also explicitly request it). Returns raw XML for
// the caller to grep/parse, since the exact capability tag can vary by
// libvirt/QEMU version on the partners' stand.
func CheckHostCapabilities(ctx context.Context) (string, error) {
	cmd := exec.CommandContext(ctx, "virsh", "capabilities")
	out, err := cmd.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("virsh capabilities: %w (output: %s)", err, string(out))
	}
	return string(out), nil
}

// EvaluateVMI runs the full health-check sequence for one VMI and returns the
// Status to persist into the run's metadata before the config-B series starts.
func EvaluateVMI(ctx context.Context, vmiName string, execInGuest func(ctx context.Context, cmd string) (string, error)) Status {
	available, detail, err := CheckGuestPMU(ctx, execInGuest)
	if err != nil {
		return Status{
			VMIName:       vmiName,
			Available:     false,
			Approximation: "host-side",
			Detail:        fmt.Sprintf("guest PMU check failed, defaulting to host-side approximation: %v", err),
		}
	}
	if !available {
		return Status{
			VMIName:       vmiName,
			Available:     false,
			Approximation: "host-side",
			Detail:        "guest /sys/devices/cpu absent — vPMU passthrough not enabled",
		}
	}
	return Status{VMIName: vmiName, Available: true, Approximation: "", Detail: detail}
}

// WriteApproximationFlag persists the per-VMI approximation decision to a small
// status file the rest of the agent (and the experiment harness) can read without
// re-running the (relatively expensive, guest-exec-based) health-check on every
// scrape. Keeping this as a plain file rather than only in Redis means the
// decision survives an agent restart mid-series.
func WriteApproximationFlag(path string, s Status) error {
	content := fmt.Sprintf("vmi=%s available=%t approximation=%q detail=%q\n",
		s.VMIName, s.Available, s.Approximation, s.Detail)
	return os.WriteFile(path, []byte(content), 0o644)
}
