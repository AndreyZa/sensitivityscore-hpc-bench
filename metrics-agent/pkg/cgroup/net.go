package cgroup

import (
	"fmt"
)

// NetStats holds the network-byte counters tracked per pod cgroup.
type NetStats struct {
	RxBytes uint64
	TxBytes uint64
}

// ReadNetStats is the integration point for per-pod network byte counters
// (docs §3.1: "Network bytes — проще брать не из perf, а из cgroup net_cls/eBPF
// socket hook отдельно"). Unlike io.stat, cgroup v2 has no built-in per-cgroup
// network byte counter file — net_cls/net_prio were cgroup v1 mechanisms being
// phased out, so the supported path on cgroup v2 hosts is an eBPF socket-level
// hook (cgroup_skb/{egress,ingress} program attached per pod cgroup) that
// accumulates byte counts into a BPF map this function reads.
//
// Left as an explicit TODO rather than a guessed implementation: the eBPF
// program itself (cgroup_skb hook + BPF map) needs to be compiled/loaded as part
// of agent startup (e.g. via cilium/ebpf), which is a meaningfully separate unit
// of work from the rest of this Go-only agent and should be scoped + reviewed on
// its own once the stand's kernel version (BTF availability) is confirmed with
// partners (see open question in Программа_экспериментов §8 re: stand specs).
func ReadNetStats(podCgroupPath string) (NetStats, error) {
	return NetStats{}, fmt.Errorf("ReadNetStats: not implemented — requires an eBPF cgroup_skb hook, see package doc comment (pod cgroup: %s)", podCgroupPath)
}
