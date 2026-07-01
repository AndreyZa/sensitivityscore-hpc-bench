// Package types defines the formal task model used throughout the dissertation:
//
//	Z = {G, R, S}
//
// where G is the job's communication/dependency graph (not modelled by this MVP
// scheduler plugin — handled at the workflow layer), R is the resource request
// vector (standard k8s requests/limits), and S is the sensitivity vector this
// package formalizes: S = (LLC, NUMA, Net, IO) ∈ [0,1]^4.
package types

// Dimension identifies one axis of the sensitivity vector S.
type Dimension int

const (
	DimLLC Dimension = iota
	DimNUMA
	DimNet
	DimIO
	numDimensions
)

func (d Dimension) String() string {
	switch d {
	case DimLLC:
		return "llc"
	case DimNUMA:
		return "numa"
	case DimNet:
		return "net"
	case DimIO:
		return "io"
	default:
		return "unknown"
	}
}

// SensitivityVector is S_job = (llc, numa, net, io) ∈ [0,1]^4 — how sensitive a job
// is to interference along each dimension. Populated from pod annotations
// (scheduling.phd/sensitivity-*), see ExtractSensitivityVector.
type SensitivityVector [numDimensions]float64

func (s SensitivityVector) Get(d Dimension) float64 { return s[d] }

// PressureVector is the current measured load/contention on a node along the same
// four dimensions — populated by metrics-agent and read by the scheduler plugin via
// NodeStateResolver + the Redis-backed metrics cache.
type PressureVector struct {
	LLCMissRate     float64 // normalized [0,1], e.g. LLC misses / 1k instr, rescaled
	NUMARemoteRatio float64 // normalized [0,1], share of remote NUMA accesses
	NetBandwidth    float64 // normalized [0,1], current utilization vs. NIC capacity
	IOPS            float64 // normalized [0,1], current utilization vs. device IOPS ceiling
	Timestamp       int64   // unix seconds, for TTL / staleness checks
	Approximation   string  // "" (exact) | "host-side" (config B, see docs §3.3)
}

// AsVector flattens PressureVector into the same [4]float64 layout as
// SensitivityVector so dotProduct() can operate generically over both.
func (p PressureVector) AsVector() [numDimensions]float64 {
	return [numDimensions]float64{p.LLCMissRate, p.NUMARemoteRatio, p.NetBandwidth, p.IOPS}
}

// Weights holds the per-dimension score weights, loaded from the
// sensitivityscore-weights ConfigMap (k8s/scheduler-config/weights-configmap.yaml)
// and hot-reloaded via fsnotify so ablation studies (Глава 3) don't require a
// scheduler restart.
type Weights struct {
	LLC  float64 `yaml:"llc_weight"`
	NUMA float64 `yaml:"numa_weight"`
	Net  float64 `yaml:"net_weight"`
	IO   float64 `yaml:"io_weight"`
}

func (w Weights) AsVector() [numDimensions]float64 {
	return [numDimensions]float64{w.LLC, w.NUMA, w.Net, w.IO}
}

func DefaultWeights() Weights {
	return Weights{LLC: 1.0, NUMA: 1.0, Net: 1.0, IO: 1.0}
}
