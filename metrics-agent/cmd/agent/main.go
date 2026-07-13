// Command agent is the DaemonSet entrypoint: one process per node, periodically
// sampling LLC PMU counters (perf_event_open) and cgroup io.stat for every pod
// scheduled on the node, aggregating into a PressureVector, and writing both the
// node-level (TTL'd, scheduler-read) and job-level (full-history, analysis-read)
// Redis keys (docs §3.2).
package main

import (
	"context"
	"errors"
	"io/fs"
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
	annoJobID             = "scheduling.phd/job-id"
	defaultSampleInterval = 5 * time.Second
	redisTTL              = 30 * time.Second
)

func main() {
	nodeName := mustEnv("NODE_NAME") // injected via fieldRef: spec.nodeName, see deploy/daemonset.yaml
	redisAddr := envOr("REDIS_ADDR", "redis.sensitivityscore-system.svc.cluster.local:6379")
	sampleInterval := sampleIntervalFromEnv()

	cfg, err := rest.InClusterConfig()
	if err != nil {
		log.Fatalf("in-cluster config: %v", err)
	}
	clientset, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		log.Fatalf("k8s client: %v", err)
	}

	// Net-калибровка стенда: NET_REFERENCE_MBPS — реально достижимый cross-node
	// aggregate (rx+tx, Mbit/s) из `make netcheck-run` (scripts/netcheck/,
	// docs §3.4). С ним появляется net_pressure = net_bw / reference ∈ [0,1] —
	// Net-ось PressureVector для скор-функции. Без него ось честно выключена
	// (net_pressure=0; сырой net_bw пишется в любом случае, для анализа).
	netRefMbps := netReferenceFromEnv()
	if netRefMbps > 0 {
		log.Printf("net calibration: NET_REFERENCE_MBPS=%.0f — net_pressure dimension ON", netRefMbps)
	} else {
		log.Printf("WARNING: NET_REFERENCE_MBPS not set — net_pressure stays 0 " +
			"(Net dimension off; run `make netcheck-run` and set the env on this DaemonSet)")
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
	// over a real window (sampleInterval) — see pkg/perf.RatioSampler.
	samplers := make(map[string]*podSampler)
	nodePSI := &nodePSISampler{}

	for range ticker.C {
		if err := sampleOnce(ctx, clientset, writer, nodeName, synth, samplers, nodePSI, netRefMbps); err != nil {
			log.Printf("sample error: %v", err)
		}
	}
}

// netPressure normalizes a raw rx+tx bytes/s rate into the [0,1] Net dimension
// using the stand's calibrated reference (Mbit/s). 0 when uncalibrated —
// the dimension is then off rather than lying with an arbitrary scale.
func netPressure(netBWBytesPerSec, refMbps float64) float64 {
	if refMbps <= 0 {
		return 0
	}
	return clamp01(netBWBytesPerSec * 8 / 1e6 / refMbps)
}

// nodePSISampler tracks the NODE-root cgroup's io.pressure across ticks. The
// node-level IO dimension is the share of wall time the node spent with at
// least one task stalled on IO — actual device contention, including
// system/non-pod IO — rather than an average of per-pod stall shares, which
// N idle pods would dilute (per-pod stalls still go to job metrics).
type nodePSISampler struct {
	last   *cgroup.IOPressureStat
	lastTS time.Time
}

// pressure returns the node's IO stall share over the window since the
// previous call; 0 on the first (baseline) call and when PSI is unavailable.
func (n *nodePSISampler) pressure() float64 {
	now := time.Now()
	psi, err := cgroup.ReadIOPressure(cgroupFSRoot)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			if !psiUnavailableWarned {
				log.Printf("WARNING: %s/io.pressure missing — PSI disabled in this kernel; "+
					"io_pressure will stay 0 (IO dimension effectively off)", cgroupFSRoot)
				psiUnavailableWarned = true
			}
		} else {
			log.Printf("node io.pressure read failed: %v (io_pressure=0 this tick)", err)
		}
		return 0
	}

	var p float64
	if n.last != nil {
		elapsedUS := float64(now.Sub(n.lastTS).Microseconds())
		if cur, last := psi.SomeTotalUS, n.last.SomeTotalUS; elapsedUS > 0 && cur >= last {
			p = float64(cur-last) / elapsedUS
		}
	}
	n.last = &psi
	n.lastTS = now
	return p
}

// podSampler holds one pod's cross-tick sampling state: the open LLC/NUMA
// perf counters (nil in synthetic mode) and the previous
// io.stat/io.pressure/net.dev readings, so llc_miss_rate, numa_remote_ratio,
// io_iops, io_pressure and net_bw are all rates/shares over the tick window
// rather than an instant/cumulative reading. The first tick for a pod only
// records baselines (sample() returns ok=false).
type podSampler struct {
	llc     *perf.RatioSampler     // nil when hardware PMU is unavailable
	numa    *perf.RatioSampler     // nil when PMU or node-level cache events are unavailable
	lastIO  *cgroup.IOStats        // nil until the first successful io.stat read
	lastPSI *cgroup.IOPressureStat // nil until the first successful io.pressure read
	lastNet *cgroup.NetStats       // nil until the first successful net.dev read
	lastTS  time.Time
	primed  bool
}

// psiUnavailableWarned / numaEventsUnavailableWarned make the corresponding
// "dimension off on this host" warnings fire once per process instead of once
// per pod per tick — like the PMU probe, these are environment properties,
// not per-pod conditions.
var (
	psiUnavailableWarned        bool
	numaEventsUnavailableWarned bool
)

func (ps *podSampler) close() {
	if ps.llc != nil {
		ps.llc.Close()
	}
	if ps.numa != nil {
		ps.numa.Close()
	}
}

// podDeltas carries the raw counter deltas behind one pod's ratio metrics for
// this tick, so sampleOnce can build the node aggregate as
// sum(numerators)/sum(denominators) — weighted by each pod's actual traffic —
// instead of averaging per-pod ratios, where one hot pod among N idle ones
// would report node pressure diluted ~N times.
type podDeltas struct {
	llcNum, llcDen   uint64
	numaNum, numaDen uint64
}

// sample reads this tick's counters and returns the pod's rates over the
// window since the previous tick, plus the raw deltas for node aggregation.
// syntheticLLC is only used when ps.llc is nil. An error means the sampler is
// stale (e.g. cgroup torn down) — the caller drops it and re-creates one next
// tick.
func (ps *podSampler) sample(cgroupPath string, syntheticLLC float64) (redisclient.Sample, podDeltas, bool, error) {
	now := time.Now()
	var deltas podDeltas

	llcRate := syntheticLLC
	llcOK := true
	if ps.llc != nil {
		var err error
		deltas.llcNum, deltas.llcDen, llcOK, err = ps.llc.SampleDeltas()
		if err != nil {
			return redisclient.Sample{}, podDeltas{}, false, err
		}
		llcRate = perf.Ratio(deltas.llcNum, deltas.llcDen)
	}

	// NUMA remote ratio: node-load-misses / node-loads over the window —
	// share of DRAM reads served by a remote NUMA node. ps.numa is nil when
	// the host has no node-event mapping (warned once at sampler creation);
	// the dimension then stays 0, same policy as PSI-less kernels for IO.
	var numaRatio float64
	if ps.numa != nil {
		var ok bool
		var err error
		deltas.numaNum, deltas.numaDen, ok, err = ps.numa.SampleDeltas()
		if err != nil {
			return redisclient.Sample{}, podDeltas{}, false, err
		}
		if ok {
			numaRatio = perf.Ratio(deltas.numaNum, deltas.numaDen)
		}
	}

	var iops float64
	io, ioErr := cgroup.ReadIOStat(cgroupPath)
	if ioErr != nil {
		// ENOENT here is a pod-teardown race (cgroup dir just vanished) —
		// normal churn, not worth a log line; same below for psi/net.
		if !errors.Is(ioErr, fs.ErrNotExist) {
			log.Printf("io.stat read failed for %s: %v (io_iops=0 this tick)", cgroupPath, ioErr)
		}
	} else {
		if ps.lastIO != nil {
			elapsed := now.Sub(ps.lastTS).Seconds()
			if cur, last := io.IOPS(), ps.lastIO.IOPS(); elapsed > 0 && cur >= last {
				iops = float64(cur-last) / elapsed
			}
		}
		ps.lastIO = &io
	}

	// IO pressure (PSI): share of the window at least one task in the pod's
	// cgroup was stalled on IO — the [0,1] IO dimension of the PressureVector
	// (raw io_iops stays as an analysis-side activity metric; it has no
	// honest [0,1] scale without a per-device max-IOPS calibration).
	var ioPressure float64
	psi, psiErr := cgroup.ReadIOPressure(cgroupPath)
	switch {
	case errors.Is(psiErr, fs.ErrNotExist):
		// Either PSI is disabled kernel-wide (nodePSISampler warns once) or
		// this is a pod-teardown race — both keep io_pressure at 0 quietly.
	case psiErr != nil:
		log.Printf("io.pressure read failed for %s: %v (io_pressure=0 this tick)", cgroupPath, psiErr)
	default:
		if ps.lastPSI != nil {
			elapsedUS := float64(now.Sub(ps.lastTS).Microseconds())
			if cur, last := psi.SomeTotalUS, ps.lastPSI.SomeTotalUS; elapsedUS > 0 && cur >= last {
				ioPressure = float64(cur-last) / elapsedUS
			}
		}
		ps.lastPSI = &psi
	}
	// Network bytes/sec — no eBPF needed after all (see cgroup.ReadNetStats
	// doc comment): all containers in a pod share one network namespace, so
	// /proc/<pid>/net/dev for any live process in it already gives pod-wide
	// totals. Analysis-only, like io_iops: raw bytes/sec has no honest [0,1]
	// scale without a per-NIC bandwidth calibration, so it doesn't feed the
	// scheduler's Net dimension (see scheduler-plugins/redis_source.go).
	var netBW float64
	net, netErr := cgroup.ReadNetStats(cgroupPath)
	if netErr != nil {
		if !errors.Is(netErr, fs.ErrNotExist) {
			log.Printf("net.dev read failed for %s: %v (net_bw=0 this tick)", cgroupPath, netErr)
		}
	} else {
		if ps.lastNet != nil {
			elapsed := now.Sub(ps.lastTS).Seconds()
			if cur, last := net.TotalBytes(), ps.lastNet.TotalBytes(); elapsed > 0 && cur >= last {
				netBW = float64(cur-last) / elapsed
			}
		}
		ps.lastNet = &net
	}
	ps.lastTS = now

	wasPrimed := ps.primed
	ps.primed = true
	if !wasPrimed || !llcOK {
		return redisclient.Sample{}, podDeltas{}, false, nil // baseline tick — no window to report yet
	}

	sample := redisclient.Sample{
		LLCMissRate:     clamp01(llcRate),
		NUMARemoteRatio: clamp01(numaRatio),
		NetBW:           netBW,
		IOIOPS:          iops,
		IOPressure:      clamp01(ioPressure),
	}
	if ps.llc == nil {
		sample.Approximation = "synthetic-devbox"
	}
	return sample, deltas, true, nil
}

// sampleOnce reads PMU/cgroup counters for every pod on this node and writes both
// the per-node aggregate (for the scheduler) and per-job history (for analysis).
// synth is non-nil when real hardware counters are unavailable in this
// environment (see main's startup probe) — the LLC value is then derived from
// it instead of perf_event_open() and tagged accordingly. samplers carries the
// per-pod cross-tick state; pods that left the node are evicted (and their
// counters closed) here.
func sampleOnce(ctx context.Context, clientset *kubernetes.Clientset, writer *redisclient.Writer, nodeName string, synth *perf.SyntheticEstimator, samplers map[string]*podSampler, nodePSI *nodePSISampler, netRefMbps float64) error {
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

	// Node aggregate semantics (the scheduler's PressureVector source):
	//   - LLC / NUMA: traffic-weighted — sum the raw counter deltas across
	//     pods and divide once. A plain mean of per-pod ratios let N idle
	//     system pods (redis, this agent, kube-system, ...) dilute one hot
	//     job's pressure ~N-fold, muting exactly the signal Score() needs.
	//   - net_bw / io_iops: summed — bandwidth and IOPS are additive.
	//   - io_pressure: taken from the node-root cgroup's PSI (see
	//     nodePSI.pressure()), i.e. actual device contention including
	//     non-pod IO, not an average of per-pod stall shares.
	// Per-JOB metrics (WriteJobMetrics) are unchanged: still the pod's own
	// ratios/rates.
	var (
		nodeAgg      redisclient.Sample
		nodeDeltas   podDeltas
		syntheticSum float64
		sampledPods  int
	)

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
				ps.llc, err = perf.NewLLCMissRatioSampler(cgroupPath)
				if err != nil {
					log.Printf("skip pod %s: open LLC counters: %v", p.name, err)
					continue
				}
				// NUMA is best-effort on top of a working PMU: generic
				// node-level cache events have no kernel mapping on some CPU
				// models — then the dimension stays 0 (warned once), while
				// LLC keeps working.
				ps.numa, err = perf.NewNUMARemoteRatioSampler(cgroupPath)
				if err != nil {
					if !numaEventsUnavailableWarned {
						log.Printf("WARNING: node-level cache events unavailable (%v) — "+
							"numa_remote_ratio will stay 0 (NUMA dimension effectively off)", err)
						numaEventsUnavailableWarned = true
					}
					ps.numa = nil
				}
			}
			samplers[p.uid] = ps
		}

		sample, deltas, ok, err := ps.sample(cgroupPath, syntheticLLC)
		if err != nil {
			// Stale sampler (pod's cgroup likely torn down mid-read) — drop
			// it; if the pod is still around, next tick re-creates it. A
			// plain teardown race (ENOENT) is normal pod churn, not worth a
			// log line every tick.
			if !errors.Is(err, fs.ErrNotExist) {
				log.Printf("skip pod %s: sampling: %v", p.name, err)
			}
			ps.close()
			delete(samplers, p.uid)
			continue
		}
		if !ok {
			continue // baseline tick for this pod, nothing to report yet
		}

		sampledPods++
		nodeDeltas.llcNum += deltas.llcNum
		nodeDeltas.llcDen += deltas.llcDen
		nodeDeltas.numaNum += deltas.numaNum
		nodeDeltas.numaDen += deltas.numaDen
		syntheticSum += sample.LLCMissRate // only meaningful in synthetic mode
		nodeAgg.NetBW += sample.NetBW
		nodeAgg.IOIOPS += sample.IOIOPS
		// Net dimension: normalized here (not in podSampler.sample) so the
		// calibration constant stays a process-level concern like the PMU probe.
		sample.NetPressure = netPressure(sample.NetBW, netRefMbps)

		if jobID, ok := p.annotations[annoJobID]; ok {
			if err := writer.WriteJobMetrics(ctx, jobID, nodeName, sample); err != nil {
				log.Printf("write job metrics for %s: %v", jobID, err)
			}
		}
	}

	if synth != nil {
		// Synthetic LLC values have no underlying counter deltas to weight
		// by — keep the old per-pod mean for the dev-box pipeline.
		if sampledPods > 0 {
			nodeAgg.LLCMissRate = syntheticSum / float64(sampledPods)
		}
		nodeAgg.Approximation = "synthetic-devbox"
	} else {
		nodeAgg.LLCMissRate = clamp01(perf.Ratio(nodeDeltas.llcNum, nodeDeltas.llcDen))
		nodeAgg.NUMARemoteRatio = clamp01(perf.Ratio(nodeDeltas.numaNum, nodeDeltas.numaDen))
	}
	nodeAgg.IOPressure = clamp01(nodePSI.pressure())
	// Node-level Net: the summed pod rx+tx rate against the same calibrated
	// reference — bandwidth is additive, so the sum is the honest node figure.
	nodeAgg.NetPressure = netPressure(nodeAgg.NetBW, netRefMbps)

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

// sampleIntervalFromEnv lets SAMPLE_INTERVAL_SECONDS override
// defaultSampleInterval — needed because short-lived pods (e.g. the harness's
// low-s profile, makespan ~1-2s) never get a single real sample at the
// default 5s cadence: the first tick after a pod appears only primes the
// baseline (see podSampler.sample), so anything shorter-lived than ~2x the
// interval is invisible to the agent regardless of how long it actually ran.
// netReferenceFromEnv reads the NET_REFERENCE_MBPS calibration (see main).
// Unset/empty means "Net dimension off" (returns 0); a malformed or negative
// value is a config error worth failing loudly on, like SAMPLE_INTERVAL_SECONDS.
func netReferenceFromEnv() float64 {
	v := os.Getenv("NET_REFERENCE_MBPS")
	if v == "" {
		return 0
	}
	mbps, err := strconv.ParseFloat(v, 64)
	if err != nil || mbps <= 0 {
		log.Fatalf("invalid NET_REFERENCE_MBPS: %q (want a positive Mbit/s figure from `make netcheck-logs`)", v)
	}
	return mbps
}

func sampleIntervalFromEnv() time.Duration {
	v := os.Getenv("SAMPLE_INTERVAL_SECONDS")
	if v == "" {
		return defaultSampleInterval
	}
	secs, err := strconv.Atoi(v)
	if err != nil || secs <= 0 {
		log.Fatalf("invalid SAMPLE_INTERVAL_SECONDS: %q", v)
	}
	return time.Duration(secs) * time.Second
}
