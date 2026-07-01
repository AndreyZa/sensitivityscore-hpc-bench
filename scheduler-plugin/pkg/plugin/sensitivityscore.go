// Package plugin implements the SensitivityScore Kubernetes Scheduler Framework
// plugin: a Score extension point plugin that ranks nodes by how much interference
// a job's sensitivity profile S_job would suffer from the node's current measured
// pressure (docs §2.1).
package plugin

import (
	"context"
	"fmt"
	"strconv"
	"sync"

	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/kubernetes/pkg/scheduler/framework"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/resolver"
	"github.com/andrey-phd/sensitivityscore-hpc-bench/scheduler-plugin/pkg/types"
)

const Name = "SensitivityScore"

// Annotation keys, per the MVP contract fixed in docs §1.3 — do not invent a new
// mechanism, the workload manifests in k8s/ already declare these.
const (
	annoLLC  = "scheduling.phd/sensitivity-llc"
	annoNUMA = "scheduling.phd/sensitivity-numa"
	annoNet  = "scheduling.phd/sensitivity-net"
	annoIO   = "scheduling.phd/sensitivity-io"
)

const (
	maxScore int64 = framework.MaxNodeScore // 100
	minScore int64 = framework.MinNodeScore // 0
)

// Args mirrors the pluginConfig.args block in
// k8s/scheduler-config/scheduler-config.yaml.
type Args struct {
	RedisAddr          string `json:"redisAddr"`
	RedisKeyTTLSeconds int    `json:"redisKeyTTLSeconds"`
	WeightsConfigPath  string `json:"weightsConfigPath"`
	NodeStateResolver  string `json:"nodeStateResolver"` // "pod-cgroup" | "qemu-process"
}

// SensitivityScorePlugin implements framework.ScorePlugin.
type SensitivityScorePlugin struct {
	handle       framework.Handle
	metricsCache resolver.MetricsReader
	nodeResolver resolver.NodeStateResolver

	mu      sync.RWMutex
	weights types.Weights // hot-reloaded from WeightsConfigPath via fsnotify, see weights_watcher.go
}

var _ framework.ScorePlugin = &SensitivityScorePlugin{}

// New is the factory the scheduler framework calls per the KubeSchedulerConfiguration
// pluginConfig entry (see cmd/scheduler/main.go for registration).
func New(_ context.Context, obj runtime.Object, h framework.Handle) (framework.Plugin, error) {
	args, ok := obj.(*Args)
	if !ok {
		return nil, fmt.Errorf("SensitivityScore: want *Args, got %T", obj)
	}

	cache := newMetricsCacheFromArgs(args)

	var nodeResolver resolver.NodeStateResolver
	switch args.NodeStateResolver {
	case "qemu-process":
		// Real KubeVirt PID-lookup wiring is environment-specific (talks to the
		// KubeVirt API / virt-launcher pods on the target cluster) — injected by
		// cmd/scheduler/main.go at startup, not constructed here.
		return nil, fmt.Errorf("SensitivityScore: qemu-process resolver must be wired in cmd/scheduler/main.go")
	case "pod-cgroup", "":
		nodeResolver = resolver.NewPodCgroupResolver(cache)
	default:
		return nil, fmt.Errorf("SensitivityScore: unknown nodeStateResolver %q", args.NodeStateResolver)
	}

	p := &SensitivityScorePlugin{
		handle:       h,
		metricsCache: cache,
		nodeResolver: nodeResolver,
		weights:      types.DefaultWeights(),
	}

	if args.WeightsConfigPath != "" {
		if err := watchWeights(args.WeightsConfigPath, p.setWeights); err != nil {
			return nil, fmt.Errorf("SensitivityScore: weights watcher: %w", err)
		}
	}

	return p, nil
}

func (p *SensitivityScorePlugin) Name() string { return Name }

func (p *SensitivityScorePlugin) setWeights(w types.Weights) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.weights = w
}

func (p *SensitivityScorePlugin) currentWeights() types.Weights {
	p.mu.RLock()
	defer p.mu.RUnlock()
	return p.weights
}

// Score implements framework.ScorePlugin. This is the formalization fixed in
// docs §2.1: score = normalize(maxScore - dot(S_job, Pressure_node) * weight).
func (p *SensitivityScorePlugin) Score(ctx context.Context, _ *framework.CycleState, pod *v1.Pod, nodeName string) (int64, *framework.Status) {
	jobProfile := extractSensitivityVector(pod.Annotations) // S_job ∈ [0,1]^4

	nodeState, err := p.nodeResolver.Resolve(nodeName)
	if err != nil {
		// No data yet (cold node, agent not warmed up) — neutral score rather
		// than failing scheduling outright; logged for the experiment harness
		// to flag in job:metrics:* as a potential measurement gap.
		return maxScore / 2, nil
	}

	interference := dotProduct(jobProfile, nodeState.AsVector(), p.currentWeights())
	score := normalize(interference)
	return score, nil
}

func (p *SensitivityScorePlugin) ScoreExtensions() framework.ScoreExtensions { return nil }

// extractSensitivityVector parses S_job = (llc, numa, net, io) ∈ [0,1]^4 from pod
// annotations. The annotation contract uses high|medium|low buckets (docs §1.3);
// they're mapped to numeric values here rather than at the manifest level so the
// score function's math stays purely numeric.
func extractSensitivityVector(annotations map[string]string) types.SensitivityVector {
	get := func(key string) float64 {
		switch annotations[key] {
		case "high":
			return 1.0
		case "medium":
			return 0.5
		case "low", "":
			return 0.0
		default:
			// Allow a raw float for finer-grained future use, fall back to 0.
			if v, err := strconv.ParseFloat(annotations[key], 64); err == nil {
				return v
			}
			return 0.0
		}
	}
	return types.SensitivityVector{
		get(annoLLC),
		get(annoNUMA),
		get(annoNet),
		get(annoIO),
	}
}

// dotProduct computes the weighted interference between a job's sensitivity
// profile and a node's current pressure: sum_d( S_job[d] * Pressure[d] * weight[d] ).
func dotProduct(jobProfile types.SensitivityVector, nodePressure [4]float64, w types.Weights) float64 {
	wv := w.AsVector()
	var sum float64
	for d := 0; d < 4; d++ {
		sum += jobProfile[d] * nodePressure[d] * wv[d]
	}
	return sum
}

// normalize maps a raw interference value (theoretical range roughly [0, sum(weights)])
// onto the framework's [MinNodeScore, MaxNodeScore] range, higher interference -> lower
// score (we want low-interference nodes preferred for placement).
func normalize(interference float64) int64 {
	// Interference is in [0, ~4*maxWeight] in practice; clamp defensively since
	// weights are operator-editable (ablation studies, docs §2.1) and could in
	// principle push the raw value outside the expected range.
	const assumedMax = 4.0 // 4 dimensions, weight=1.0 each, S and Pressure both in [0,1]
	normalized := interference / assumedMax
	if normalized < 0 {
		normalized = 0
	}
	if normalized > 1 {
		normalized = 1
	}
	score := float64(maxScore) - normalized*float64(maxScore-minScore)
	return int64(score)
}
