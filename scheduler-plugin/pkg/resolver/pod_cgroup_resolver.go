package resolver

import "github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/types"

// PodCgroupResolver is used for config A (K8s bare-metal): metrics-agent already
// keys node:metrics:<nodeName> by aggregating PMU/cgroup readings across the pods
// actually running on that node, so resolution here is a direct pass-through read.
// This is the default resolver (nodeStateResolver: "pod-cgroup" in
// k8s/scheduler-config/scheduler-config.yaml).
type PodCgroupResolver struct {
	reader MetricsReader
}

func NewPodCgroupResolver(reader MetricsReader) *PodCgroupResolver {
	return &PodCgroupResolver{reader: reader}
}

func (r *PodCgroupResolver) Resolve(nodeName string) (types.PressureVector, error) {
	return r.reader.GetNodePressure(nodeName)
}
