// Command agent is the DaemonSet entrypoint: one process per node, periodically
// sampling LLC PMU counters (perf_event_open) and cgroup io.stat for every pod
// scheduled on the node, aggregating into a PressureVector, and writing both the
// node-level (TTL'd, scheduler-read) and job-level (full-history, analysis-read)
// Redis keys (docs §3.2).
package main

import (
	"context"
	"log"
	"os"
	"strconv"
	"time"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/metrics-agent/pkg/cgroup"
	"github.com/andrey-phd/sensitivityscore-hpc-bench/metrics-agent/pkg/perf"
	"github.com/andrey-phd/sensitivityscore-hpc-bench/metrics-agent/pkg/redisclient"
)

const (
	annoJobID      = "scheduling.phd/job-id"
	sampleInterval = 5 * time.Second
	redisTTL       = 30 * time.Second
)

func main() {
	nodeName := mustEnv("NODE_NAME") // injected via fieldRef: spec.nodeName, see deploy/daemonset.yaml
	redisAddr := envOr("REDIS_ADDR", "redis.sensitivityscore-system.svc.cluster.local:6379")

	cfg, err := rest.InClusterConfig()
	if err != nil {
		log.Fatalf("in-cluster config: %v", err)
	}
	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		log.Fatalf("k8s client: %v", err)
	}

	writer := redisclient.NewWriter(redisAddr, redisTTL)
	defer writer.Close()

	ctx := context.Background()
	ticker := time.NewTicker(sampleInterval)
	defer ticker.Stop()

	// Local-dev fallback (see pkg/perf/synthetic.go): WSL2/Docker Desktop don't
	// expose a vPMU, so perf_event_open() fails with EINVAL regardless of
	// capabilities/paranoid sysctl. Probe once at startup rather than failing
	// per-pod every tick, and tag every sample so this is never mistaken for
	// real config-A measurements.
	hwPMUAvailable, probeErr := perf.ProbeHardwareCounters("/sys/fs/cgroup")
	var synth *perf.SyntheticEstimator
	if !hwPMUAvailable {
		log.Printf("WARNING: hardware PMU counters unavailable (%v) — falling back to synthetic "+
			"LLC values (docs §3.3-style approximation, tag=synthetic-devbox) for local Redis-pipeline "+
			"development only; NOT valid for dissertation measurements", probeErr)
		synth = &perf.SyntheticEstimator{}
	}

	log.Printf("metrics-agent starting on node=%s, sampling every %s", nodeName, sampleInterval)

	// Per-pod sampling state, keyed by pod UID, kept across ticks: perf
	// counters must stay open between ticks so each tick measures the delta
	// over a real window (sampleInterval) — see pkg/perf.CgroupLLCSampler.
	samplers := make(map[string]*podSampler)

	for range ticker.C {
		if err := sampleOnce(ctx, clientset, writer, nodeName, synth, samplers); err != nil {
			log.Printf("sample error: %v", err)
		}
	}
}

// podSampler holds one pod's cross-tick sampling state: the open LLC perf
// counters (nil in synthetic mode) and the previous io.stat reading, so both
// llc_miss_rate and io_iops are rates over the tick window rather than an
// instant/cumulative reading. The first tick for a pod only records baselines
// (sample() returns ok=false).
type podSampler struct {
	llc    *perf.CgroupLLCSampler // nil when hardware PMU is unavailable
	lastIO *cgroup.IOStats        // nil until the first successful io.stat read
	lastTS time.Time
	primed bool
}

func (ps *podSampler) close() {
	if ps.llc != nil {
		ps.llc.Close()
	}
}

// sample reads this tick's counters and returns the pod's rates over the
// window since the previous tick. syntheticLLC is only used when ps.llc is
// nil. An error means the sampler is stale (e.g. cgroup torn down) — the
// caller drops it and re-creates one next tick.
func (ps *podSampler) sample(cgroupPath string, syntheticLLC float64) (redisclient.Sample, bool, error) {
	now := time.Now()

	llcRate := syntheticLLC
	llcOK := true
	if ps.llc != nil {
		var err error
		llcRate, llcOK, err = ps.llc.SampleRate()
		if err != nil {
			return redisclient.Sample{}, false, err
		}
	}

	var iops float64
	io, ioErr := cgroup.ReadIOStat(cgroupPath)
	if ioErr != nil {
		log.Printf("io.stat read failed for %s: %v (io_iops=0 this tick)", cgroupPath, ioErr)
	} else {
		if ps.lastIO != nil {
			elapsed := now.Sub(ps.lastTS).Seconds()
			if cur, last := io.IOPS(), ps.lastIO.IOPS(); elapsed > 0 && cur >= last {
				iops = float64(cur-last) / elapsed
			}
		}
		ps.lastIO = &io
	}
	ps.lastTS = now

	wasPrimed := ps.primed
	ps.primed = true
	if !wasPrimed || !llcOK {
		return redisclient.Sample{}, false, nil // baseline tick — no window to report yet
	}

	sample := redisclient.Sample{
		LLCMissRate:     clamp01(llcRate),
		NUMARemoteRatio: 0, // see perf.ReadUncoreNUMABandwidth TODO
		NetBW:           0, // see cgroup.ReadNetStats TODO
		IOIOPS:          iops,
	}
	if ps.llc == nil {
		sample.Approximation = "synthetic-devbox"
	}
	return sample, true, nil
}

// sampleOnce reads PMU/cgroup counters for every pod on this node and writes both
// the per-node aggregate (for the scheduler) and per-job history (for analysis).
// synth is non-nil when real hardware counters are unavailable in this
// environment (see main's startup probe) — the LLC value is then derived from
// it instead of perf_event_open() and tagged accordingly. samplers carries the
// per-pod cross-tick state; pods that left the node are evicted (and their
// counters closed) here.
func sampleOnce(ctx context.Context, clientset *kubernetes.Clientset, writer *redisclient.Writer, nodeName string, synth *perf.SyntheticEstimator, samplers map[string]*podSampler) error {
	pods, err := listLocalPods(ctx, clientset, nodeName)
	if err != nil {
		return err
	}

	seen := make(map[string]bool, len(pods))
	for _, p := range pods {
		seen[p.uid] = true
	}
	for uid, ps := range samplers {
		if !seen[uid] {
			ps.close()
			delete(samplers, uid)
		}
	}

	var syntheticLLC float64
	if synth != nil {
		syntheticLLC, err = synth.NextRatio()
		if err != nil {
			log.Printf("synthetic estimator: %v (using 0)", err)
			syntheticLLC = 0
		}
	}

	var nodeAgg redisclient.Sample
	var sampledPods int

	for _, p := range pods {
		cgroupPath, err := resolvePodCgroupPath(p)
		if err != nil {
			log.Printf("skip pod %s: cgroup resolution: %v", p.name, err)
			continue
		}

		ps, exists := samplers[p.uid]
		if !exists {
			ps = &podSampler{}
			if synth == nil {
				ps.llc, err = perf.NewCgroupLLCSampler(cgroupPath)
				if err != nil {
					log.Printf("skip pod %s: open LLC counters: %v", p.name, err)
					continue
				}
			}
			samplers[p.uid] = ps
		}

		sample, ok, err := ps.sample(cgroupPath, syntheticLLC)
		if err != nil {
			// Stale sampler (pod's cgroup likely torn down mid-read) — drop
			// it; if the pod is still around, next tick re-creates it.
			log.Printf("skip pod %s: sampling: %v", p.name, err)
			ps.close()
			delete(samplers, p.uid)
			continue
		}
		if !ok {
			continue // baseline tick for this pod, nothing to report yet
		}

		sampledPods++
		nodeAgg.LLCMissRate += sample.LLCMissRate
		nodeAgg.NUMARemoteRatio += sample.NUMARemoteRatio
		nodeAgg.NetBW += sample.NetBW
		nodeAgg.IOIOPS += sample.IOIOPS

		if jobID, ok := p.annotations[annoJobID]; ok {
			if err := writer.WriteJobMetrics(ctx, jobID, nodeName, sample); err != nil {
				log.Printf("write job metrics for %s: %v", jobID, err)
			}
		}
	}

	if sampledPods > 0 {
		nodeAgg.LLCMissRate /= float64(sampledPods)
		nodeAgg.NUMARemoteRatio /= float64(sampledPods)
		nodeAgg.NetBW /= float64(sampledPods)
		nodeAgg.IOIOPS /= float64(sampledPods)
	}
	if synth != nil {
		nodeAgg.Approximation = "synthetic-devbox"
	}

	return writer.WriteNodeMetrics(ctx, nodeName, nodeAgg)
}

func clamp01(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		log.Fatalf("required env var %s not set", key)
	}
	return v
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func init() {
	// fail fast on obviously-misconfigured sample intervals if ever made
	// configurable via env in the future
	if v := os.Getenv("SAMPLE_INTERVAL_SECONDS"); v != "" {
		if _, err := strconv.Atoi(v); err != nil {
			log.Fatalf("invalid SAMPLE_INTERVAL_SECONDS: %v", err)
		}
	}
}
