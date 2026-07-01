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

	log.Printf("metrics-agent starting on node=%s, sampling every %s", nodeName, sampleInterval)

	for range ticker.C {
		if err := sampleOnce(ctx, clientset, writer, nodeName); err != nil {
			log.Printf("sample error: %v", err)
		}
	}
}

// sampleOnce reads PMU/cgroup counters for every pod on this node and writes both
// the per-node aggregate (for the scheduler) and per-job history (for analysis).
func sampleOnce(ctx context.Context, clientset *kubernetes.Clientset, writer *redisclient.Writer, nodeName string) error {
	pods, err := listLocalPods(ctx, clientset, nodeName)
	if err != nil {
		return err
	}

	var nodeAgg redisclient.Sample
	var sampledPods int

	for _, p := range pods {
		cgroupPath, err := resolvePodCgroupPath(p)
		if err != nil {
			log.Printf("skip pod %s: cgroup resolution: %v", p.name, err)
			continue
		}

		sample, err := sampleCgroup(cgroupPath)
		if err != nil {
			log.Printf("skip pod %s: sampling: %v", p.name, err)
			continue
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

	return writer.WriteNodeMetrics(ctx, nodeName, nodeAgg)
}

// sampleCgroup reads the LLC PMU counters + io.stat for a single pod cgroup and
// returns a normalized Sample. NUMA bandwidth and network bytes are left at zero
// pending the uncore-PMU and eBPF integration points documented in
// pkg/perf.ReadUncoreNUMABandwidth and pkg/cgroup.ReadNetStats.
func sampleCgroup(cgroupPath string) (redisclient.Sample, error) {
	fd, err := perf.OpenPodCgroup(cgroupPath)
	if err != nil {
		return redisclient.Sample{}, err
	}
	missCounter, err := perf.LLCMissesCounter(fd)
	if err != nil {
		return redisclient.Sample{}, err
	}
	defer missCounter.Close()
	refCounter, err := perf.LLCReferencesCounter(fd)
	if err != nil {
		return redisclient.Sample{}, err
	}
	defer refCounter.Close()

	missCounter.Enable()
	refCounter.Enable()
	misses, err := missCounter.Read()
	if err != nil {
		return redisclient.Sample{}, err
	}
	refs, err := refCounter.Read()
	if err != nil {
		return redisclient.Sample{}, err
	}

	llcMissRate := 0.0
	if refs > 0 {
		llcMissRate = float64(misses) / float64(refs)
	}

	ioStats, err := cgroup.ReadIOStat(cgroupPath)
	if err != nil {
		log.Printf("io.stat read failed for %s: %v (continuing with io_iops=0)", cgroupPath, err)
	}

	return redisclient.Sample{
		LLCMissRate:     clamp01(llcMissRate),
		NUMARemoteRatio: 0, // see perf.ReadUncoreNUMABandwidth TODO
		NetBW:           0, // see cgroup.ReadNetStats TODO
		IOIOPS:          float64(ioStats.IOPS()),
	}, nil
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
