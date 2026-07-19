// Package promexport публикует узловой PressureVector, который агент и так
// считает каждый тик для Redis, ещё и как Prometheus-гейджи — чтобы оси
// чувствительности были видны в Grafana живьём во время серии, а не только
// постфактум в Parquet.
//
// Redis остаётся единственным авторитетным путём: hot-path планировщика и
// экспорт харнесса читают ТОЛЬКО его (docs §3.2). Здесь — read-only зеркало
// для наблюдаемости: ошибка скрейпа или упавший HTTP-сервер не должны влиять
// на сэмплирование, поэтому Publish никогда не возвращает ошибку, а Serve
// живёт в своей горутине.
package promexport

import (
	"log"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/andrey-phd/sensitivityscore-hpc-bench/metrics-agent/pkg/redisclient"
)

// DefaultAddr — порт /metrics агента. Не 9100: там node_exporter, а на
// bench-узлах агент — единственный источник метрик (node_exporter туда
// намеренно не ставится, чтобы не шуметь в LLC/IO измерительных узлов,
// docs «Ввод прод-стенда» §2).
const DefaultAddr = ":9101"

// Exporter держит гейджи одного узла. Метки узла проставляются один раз через
// WrapRegistererWith, а не аргументом каждого Set: агент — DaemonSet, один
// процесс = ровно один узел, и node как константная метка исключает
// рассинхрон между сериями метрик.
type Exporter struct {
	reg *prometheus.Registry

	// Оси PressureVector — те же величины, что уходят в node:metrics:<node>.
	llcMissRate     prometheus.Gauge
	llcMissesPerSec prometheus.Gauge
	numaRemoteRatio prometheus.Gauge
	netBW           prometheus.Gauge
	netPressure     prometheus.Gauge
	ioIOPS          prometheus.Gauge
	ioPressure      prometheus.Gauge

	// Операционные — «честен ли сбор на этом узле». Без них дашборд покажет
	// нули и не отличит «нагрузки нет» от «датчик выключен».
	sampledPods  prometheus.Gauge
	lastSampleTS prometheus.Gauge
	sampleErrors prometheus.Counter
	writeErrors  prometheus.Counter

	pmuHardware   prometheus.Gauge
	pmuMultiplex  prometheus.Gauge
	netCalibrated prometheus.Gauge
	llcCalibrated prometheus.Gauge
	psiAvailable  prometheus.Gauge
}

// New собирает экспортёр с собственным реестром (не DefaultRegisterer): в него
// кладутся только go/process-коллекторы и метрики стенда, без глобального
// мусора от библиотек.
func New(nodeName string) *Exporter {
	reg := prometheus.NewRegistry()
	r := prometheus.WrapRegistererWith(prometheus.Labels{"node": nodeName}, reg)

	gauge := func(name, help string) prometheus.Gauge {
		g := prometheus.NewGauge(prometheus.GaugeOpts{Name: name, Help: help})
		r.MustRegister(g)
		return g
	}
	counter := func(name, help string) prometheus.Counter {
		c := prometheus.NewCounter(prometheus.CounterOpts{Name: name, Help: help})
		r.MustRegister(c)
		return c
	}

	reg.MustRegister(
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)

	return &Exporter{
		reg: reg,

		llcMissRate: gauge("ss_node_llc_miss_rate",
			"Node LLC pressure in [0,1]. Calibrated stand: llc_misses_per_sec / LLC_REFERENCE_MISSES_PER_SEC. "+
				"UNCALIBRATED (ss_agent_llc_calibrated=0) it is the raw misses/references ratio, which INVERTS "+
				"under streaming load - a loaded node then looks cleaner than an idle one. Do not chart it as "+
				"pressure without checking the calibration gauge."),
		llcMissesPerSec: gauge("ss_node_llc_misses_per_sec",
			"Raw node LLC misses per second (traffic-weighted sum of per-pod counter deltas). Monotonic in load, "+
				"unlike the ratio - this is the field to read under a 2x stress-ng --stream reference storm to "+
				"obtain LLC_REFERENCE_MISSES_PER_SEC."),
		numaRemoteRatio: gauge("ss_node_numa_remote_ratio",
			"Share of DRAM reads served by a remote NUMA node, [0,1]. Stays 0 where the CPU has no kernel "+
				"mapping for node-level cache events (single-NUMA STAGE nodes included)."),
		netBW: gauge("ss_node_net_bw_bytes_per_second",
			"Raw node rx+tx rate in bytes/s, summed over pods. Analysis-side activity metric - additive, "+
				"always recorded regardless of calibration."),
		netPressure: gauge("ss_node_net_pressure",
			"Net dimension of the PressureVector, [0,1] - net_bw against NET_REFERENCE_MBPS. Exactly 0 when "+
				"uncalibrated (ss_agent_net_calibrated=0): the axis is then off rather than lying with an "+
				"arbitrary scale."),
		ioIOPS: gauge("ss_node_io_iops",
			"Raw node IO operations per second, summed over pods. Analysis-side activity metric: it has no "+
				"honest [0,1] scale without a per-device max-IOPS calibration."),
		ioPressure: gauge("ss_node_io_pressure",
			"IO dimension of the PressureVector, [0,1] - PSI io.pressure 'some' share of the tick window on the "+
				"node-root cgroup, i.e. real device contention including non-pod IO. Stays 0 on kernels without "+
				"PSI (see ss_agent_psi_available)."),

		sampledPods: gauge("ss_agent_sampled_pods",
			"Pods that produced a real (non-baseline) sample on the last tick. A pod's first tick only primes "+
				"counters, so anything shorter-lived than ~2x SAMPLE_INTERVAL_SECONDS never appears here."),
		lastSampleTS: gauge("ss_agent_last_sample_timestamp_seconds",
			"Unix timestamp of the last completed sampling tick. Alert on staleness: the scheduler reads a "+
				"30s-TTL Redis key, so a stall here means the plugin is scoring on expired data."),
		sampleErrors: counter("ss_agent_sample_errors_total",
			"Ticks that ended in an error: pod listing, node aggregation, or the final node-metrics Redis write. "+
				"Per-pod cgroup teardown races are NOT counted (normal churn). A failing Redis write raises this "+
				"AND ss_agent_redis_write_errors_total - compare the two to tell a collection fault from a Redis fault."),
		writeErrors: counter("ss_agent_redis_write_errors_total",
			"Failed Redis writes of node/job metrics. Non-zero means the scheduler hot-path is losing data even "+
				"if this /metrics endpoint still looks healthy."),

		pmuHardware: gauge("ss_agent_pmu_hardware_available",
			"1 when perf_event_open() gives honest hardware counters on this node, 0 when the agent fell back to "+
				"synthetic LLC values (dev-box only - NOT valid for dissertation measurements)."),
		netCalibrated: gauge("ss_agent_net_calibrated",
			"1 when NET_REFERENCE_MBPS is set, i.e. the Net axis is on. See `make netcheck-run`."),
		llcCalibrated: gauge("ss_agent_llc_calibrated",
			"1 when LLC_REFERENCE_MISSES_PER_SEC is set. 0 means ss_node_llc_miss_rate is the raw, "+
				"load-inverting ratio."),
		pmuMultiplex: gauge("ss_agent_pmu_multiplex_ratio",
			"Share of the sampling window the PMU counters were actually scheduled on hardware, worst pod of the "+
				"last tick (running/enabled). 1 = no multiplexing. Below 1 the kernel time-sliced the events and "+
				"the agent scaled the raw counts up by enabled/running to compensate - the numbers are corrected "+
				"but no longer directly measured. The number of open events grows with the number of pods, i.e. "+
				"with the experimental variable, so a low ratio biases llc_misses_per_sec exactly where load is "+
				"highest. Alert below 0.9 (SSPMUMultiplexed). Stays 1 in synthetic mode - no counters were open."),
		psiAvailable: gauge("ss_agent_psi_available",
			"1 when the kernel exposes cgroup io.pressure (PSI). 0 means the IO axis is effectively off - "+
				"Debian/RHEL builds need psi=1 on the kernel cmdline."),
	}
}

// SetEnvironment фиксирует свойства узла, известные на старте: они не меняются
// между тиками, но именно по ним дашборд отличает «оси нулевые, потому что
// тихо» от «оси нулевые, потому что датчик выключен».
func (e *Exporter) SetEnvironment(pmuHardware bool, netRefMbps, llcRefMps float64) {
	e.pmuHardware.Set(b2f(pmuHardware))
	e.netCalibrated.Set(b2f(netRefMbps > 0))
	e.llcCalibrated.Set(b2f(llcRefMps > 0))
}

// SetPMUMultiplexRatio публикует долю окна, которую счётчики реально простояли
// на PMU (худший под тика). Отдельный сеттер, а не поле Publish: величина
// относится к достоверности сбора, а не к вектору давления, и в синтетическом
// режиме не выставляется вовсе.
func (e *Exporter) SetPMUMultiplexRatio(ratio float64) { e.pmuMultiplex.Set(ratio) }

// SetPSIAvailable выставляется по факту первого чтения io.pressure, а не на
// старте: PSI определяется наличием файла, и агент узнаёт об этом только когда
// nodePSISampler впервые сходит в cgroupfs.
func (e *Exporter) SetPSIAvailable(ok bool) { e.psiAvailable.Set(b2f(ok)) }

// Publish зеркалит узловой агрегат тика. Вызывается там же, где
// WriteNodeMetrics — из одной горутины сэмплирования, поэтому гейджи
// обновляются согласованно (client_golang сам по себе потокобезопасен, но
// «все оси от одного тика» гарантирует именно единственный вызывающий).
func (e *Exporter) Publish(s redisclient.Sample, sampledPods int) {
	e.llcMissRate.Set(s.LLCMissRate)
	e.llcMissesPerSec.Set(s.LLCMissesPerSec)
	e.numaRemoteRatio.Set(s.NUMARemoteRatio)
	e.netBW.Set(s.NetBW)
	e.netPressure.Set(s.NetPressure)
	e.ioIOPS.Set(s.IOIOPS)
	e.ioPressure.Set(s.IOPressure)

	e.sampledPods.Set(float64(sampledPods))
	e.lastSampleTS.Set(float64(time.Now().Unix()))
}

// ObserveSampleError / ObserveWriteError — счётчики отказов; сама по себе
// ошибка уже логируется вызывающим, здесь она становится алертируемой.
func (e *Exporter) ObserveSampleError() { e.sampleErrors.Inc() }
func (e *Exporter) ObserveWriteError()  { e.writeErrors.Inc() }

// Serve поднимает /metrics. Блокирующий — вызывать в отдельной горутине.
// Падение HTTP-сервера логируется, но не валит агент: сбор метрик для
// планировщика важнее наблюдаемости за ним.
func (e *Exporter) Serve(addr string) {
	mux := http.NewServeMux()
	mux.Handle("/metrics", promhttp.HandlerFor(e.reg, promhttp.HandlerOpts{}))
	// Отдельный liveness-путь, чтобы проба не тащила весь набор метрик.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})

	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("prometheus exporter listening on %s/metrics", addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Printf("prometheus exporter stopped: %v (sampling continues)", err)
	}
}

func b2f(b bool) float64 {
	if b {
		return 1
	}
	return 0
}
