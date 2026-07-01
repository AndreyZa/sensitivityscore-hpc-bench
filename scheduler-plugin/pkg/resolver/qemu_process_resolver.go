package resolver

import (
	"context"
	"fmt"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/types"
)

// KubeVirtPIDLookup abstracts the KubeVirt-specific part of the VMI -> qemu-kvm PID
// -> cgroup path chain (docs §2.2, §3.3). Kept as an interface so the resolver can
// be unit-tested without a live cluster; production wiring talks to the KubeVirt
// API (VirtualMachineInstance status -> virt-launcher pod -> /proc/<pid>/cgroup).
type KubeVirtPIDLookup interface {
	// QemuPIDForNode returns the qemu-kvm process PID(s) running on nodeName,
	// keyed by the VMI's scheduling.phd/job-id annotation so per-job pressure
	// can be distinguished when multiple VMIs share a node.
	QemuPIDForNode(ctx context.Context, nodeName string) (map[string]int, error)
	// VPMUAvailable reports whether vPMU passthrough is enabled+working for the
	// given VMI (health-checked at series start per docs §3.3). When false, the
	// resolved PressureVector.Approximation is set to "host-side".
	VPMUAvailable(ctx context.Context, jobID string) (bool, error)
}

// QemuProcessResolver is used for config B (KubeVirt): the plugin sees a
// virt-launcher pod, not the real workload — so node pressure must be attributed
// via the qemu-kvm host process's cgroup rather than the pod's own cgroup.
// metrics-agent already performs this same mapping when *collecting* metrics
// (docs §3.3); this resolver performs the equivalent mapping on the *read* side so
// the plugin can validate which job a given node-level pressure reading actually
// corresponds to, and propagate the approximation flag into the score decision.
type QemuProcessResolver struct {
	reader MetricsReader
	lookup KubeVirtPIDLookup
	ctx    context.Context
}

func NewQemuProcessResolver(ctx context.Context, reader MetricsReader, lookup KubeVirtPIDLookup) *QemuProcessResolver {
	return &QemuProcessResolver{ctx: ctx, reader: reader, lookup: lookup}
}

func (r *QemuProcessResolver) Resolve(nodeName string) (types.PressureVector, error) {
	pressure, err := r.reader.GetNodePressure(nodeName)
	if err != nil {
		return types.PressureVector{}, fmt.Errorf("qemu-process resolver: read node pressure: %w", err)
	}

	// Confirm there is at least one qemu-kvm process resolvable on this node —
	// if not, the node:metrics:* entry may be stale or sourced from a non-VMI
	// pod, which would be a config error for a config-B run.
	pids, err := r.lookup.QemuPIDForNode(r.ctx, nodeName)
	if err != nil {
		return types.PressureVector{}, fmt.Errorf("qemu-process resolver: PID lookup: %w", err)
	}
	if len(pids) == 0 {
		return types.PressureVector{}, fmt.Errorf("qemu-process resolver: no qemu-kvm process found on node %q", nodeName)
	}

	// vPMU availability is per-VMI, but for the node-level Score() decision we
	// only need to know whether *any* co-located VMI on this node is running in
	// approximated (host-side) mode — if so, conservatively mark the whole
	// reading as an approximation so downstream analysis can filter/flag it.
	approximation := ""
	for jobID := range pids {
		ok, err := r.lookup.VPMUAvailable(r.ctx, jobID)
		if err != nil {
			continue // best-effort: missing health-check data shouldn't block scoring
		}
		if !ok {
			approximation = "host-side"
			break
		}
	}
	pressure.Approximation = approximation

	return pressure, nil
}
