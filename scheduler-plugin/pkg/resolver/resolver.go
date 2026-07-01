// Package resolver implements the NodeStateResolver abstraction (docs §2.2):
// the SensitivityScore plugin must not special-case config A (bare pod) vs config B
// (KubeVirt VMI) with if-branches — instead it depends on this interface, with one
// implementation per deployment shape.
package resolver

import "github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/types"

// NodeStateResolver resolves a node's current PressureVector. Implementations differ
// only in *where* they look up the underlying cgroup/process whose metrics
// metrics-agent has written to Redis — the plugin's Score() function is agnostic.
type NodeStateResolver interface {
	// Resolve returns the current pressure vector for the given node, as last
	// written by metrics-agent to Redis key node:metrics:<nodeName>.
	Resolve(nodeName string) (types.PressureVector, error)
}

// MetricsReader is the minimal Redis read surface NodeStateResolver
// implementations need — kept as an interface so tests can fake it without a real
// Redis instance.
type MetricsReader interface {
	GetNodePressure(nodeName string) (types.PressureVector, error)
}
