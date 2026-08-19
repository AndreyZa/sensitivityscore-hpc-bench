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
	numaDRAMRate    prometheus.Gauge
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

	// Энерговетка (P0): накопительные RAPL-джоули по powercap-зонам узла.
	raplJoules *prometheus.CounterVec
	raplZones  prometheus.Gauge

	// Тепловой троттлинг и частота ядер (pkg/cputhrottle): признак того, что
	// узел работал не на полную, а замедление жертвы объясняется не только
	// интерференцией.
	throttleEvents *prometheus.CounterVec
	cpuFreq        *prometheus.GaugeVec
	throttleAvail  prometheus.Gauge
	freqAvail      prometheus.Gauge
	freqSource     *prometheus.GaugeVec
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
	counterVec := func(name, help string, labels []string) *prometheus.CounterVec {
		c := prometheus.NewCounterVec(prometheus.CounterOpts{Name: name, Help: help}, labels)
		r.MustRegister(c)
		return c
	}
	gaugeVec := func(name, help string, labels []string) *prometheus.GaugeVec {
		g := prometheus.NewGaugeVec(prometheus.GaugeOpts{Name: name, Help: help}, labels)
		r.MustRegister(g)
		return g
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
				"mapping for node-level cache events (single-NUMA STAGE nodes included), AND when the node's "+
				"DRAM read rate is below NUMA_MIN_DRAM_EVENTS_PER_SEC: a cache-resident workload makes a few "+
				"thousand node events/s of kernel/shared-page background whose remote share is meaningless "+
				"(measured 0.97 on an idle pinned victim, 19.08.2026). Check ss_node_numa_dram_events_per_sec "+
				"to see which regime the node is in."),
		numaDRAMRate: gauge("ss_node_numa_dram_events_per_sec",
			"Raw node-loads+node-load-misses per second summed over pods - the denominator behind "+
				"ss_node_numa_remote_ratio. Below NUMA_MIN_DRAM_EVENTS_PER_SEC the ratio is gated to 0; "+
				"genuine DRAM-bound traffic (stream storm, memcpy) runs millions of events/s."),
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
				"the agent scales the raw counts up by enabled/running - but measured on real hardware that "+
				"extrapolation is unbiased only on average: at a 0.58 duty cycle the same workload came out "+
				"between -3% and +1831% of its uncontended value. Treat this gauge as a VALIDITY GATE for the "+
				"cache axis, not as reassurance that the numbers were fixed; the only real remedy is fewer "+
				"simultaneously open events. Alert below 0.9 (SSPMUMultiplexed). Stays 1 in synthetic mode."),
		psiAvailable: gauge("ss_agent_psi_available",
			"1 when the kernel exposes cgroup io.pressure (PSI). 0 means the IO axis is effectively off - "+
				"Debian/RHEL builds need psi=1 on the kernel cmdline."),

		raplJoules: counterVec("ss_node_rapl_joules_total",
			"Cumulative Intel RAPL energy per powercap zone, joules, wraparound-corrected (pkg/rapl). "+
				"Energy for a window = counter difference at the window borders (scripts/energy-window.py, "+
				"source rapl-pkg/rapl-dram) - do NOT integrate instantaneous power. Domain names repeat "+
				"across sockets (dram on both), so identity is the `zone` label (sysfs id); `domain` is "+
				"the human name (package-0, dram, psys). Absent on nodes without powercap (VMs) - see "+
				"ss_agent_rapl_zones.",
			[]string{"zone", "domain"}),
		raplZones: gauge("ss_agent_rapl_zones",
			"Powercap zones the agent actually samples. 0 = RAPL unavailable (VM, or no permission to "+
				"read energy_uj) - the energy branch must not trust rapl totals from such a node; "+
				"bare-metal 2-socket SPR is expected to show 5 (2x package + 2x dram + psys)."),

		throttleEvents: counterVec("ss_node_cpu_throttle_events_total",
			"Thermal throttle events on the node, deduplicated by topology (pkg/cputhrottle). Any "+
				"increase during a run means the CPU ran below full speed and the victim's runtime got "+
				"longer for a reason that is NOT interference - the measured slowdown is then partly an "+
				"artefact. scope=core counts per-core events, scope=package per-socket ones; the sysfs "+
				"files repeat across siblings, so a naive sum would multiply the package counter by the "+
				"core count. Absent on VMs - see ss_agent_cpu_throttle_available.",
			[]string{"scope"}),
		cpuFreq: gaugeVec("ss_node_cpu_freq_hertz",
			"Current core frequency on the node, hertz (pkg/cputhrottle). Sampled on its OWN slow cycle "+
				"(CPU_FREQ_INTERVAL_SECONDS, 60s by default), not every tick: reading it costs an IPI or "+
				"an MSR read per core, and the measured cores should not be poked every 5 seconds for a "+
				"number that only says HOW MUCH the throttling cost. Read stat=max together with "+
				"stat=min: the stand leaves most cores idle (28 CPUs of 64 go to the victim), so the "+
				"minimum always shows an idle core and the average is diluted by them - the maximum is "+
				"what the busy cores actually reached. See ss_agent_cpu_freq_source for where the number "+
				"comes from: on the prod bench nodes there is no cpufreq at all and the value is the "+
				"kernel's APERF/MPERF-derived effective frequency from /proc/cpuinfo.",
			[]string{"stat"}),
		freqSource: gaugeVec("ss_agent_cpu_freq_source",
			"Where the frequency reading comes from: source=cpufreq (scaling_cur_freq, the value the "+
				"driver was asked for) or source=cpuinfo (the kernel's effective frequency computed from "+
				"APERF/MPERF). They are not the same quantity, so the label exists to keep them apart. On "+
				"the prod bench nodes (Dell R760, 19.08.2026) it is cpuinfo: P-states are managed by the "+
				"BIOS and no cpufreq driver loads - yet the cores DO scale (785-797 MHz observed at idle "+
				"against a 2800 MHz nominal), so 'BIOS-managed' must not be read as 'pinned at maximum'.",
			[]string{"source"}),
		freqAvail: gauge("ss_agent_cpu_freq_available",
			"1 when the node exposes per-core scaling_cur_freq. 0 means the cpufreq directory is absent "+
				"entirely - on the prod bench nodes (Dell R760, 19.08.2026) that is the case: P-states are "+
				"managed by the BIOS, not the OS, so there is no frequency to read. For the measurements "+
				"that is a good property (no OS-driven frequency variance between runs), but it also means "+
				"the thermal cost can only be seen as EVENTS, not as a frequency drop."),
		throttleAvail: gauge("ss_agent_cpu_throttle_available",
			"1 when the node exposes thermal_throttle counters in sysfs. 0 on VMs (ss-system, STAGE) - "+
				"the thermal signal is then absent rather than zero, and a run on such a node cannot be "+
				"cleared of thermal artefacts at all."),
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

// SetRAPLZones — сколько powercap-зон агент реально читает (0 = RAPL на узле
// нет: ВМ или нет прав). Выставляется один раз на старте, по итогам Discover.
func (e *Exporter) SetRAPLZones(n int) { e.raplZones.Set(float64(n)) }

// SetNUMADRAMEventsPerSec publishes the node's raw DRAM-read event rate — the
// validity context for the gated numa_remote_ratio (see the gauge help).
func (e *Exporter) SetNUMADRAMEventsPerSec(rate float64) { e.numaDRAMRate.Set(rate) }

// SetCPUThrottleAvailable — есть ли на узле счётчики теплового троттлинга.
// Выставляется один раз на старте: в ВМ их нет, и сигнал тогда ОТСУТСТВУЕТ, а
// не равен нулю — разницу обязан видеть тот, кто читает дашборд.
func (e *Exporter) SetCPUThrottleAvailable(ok bool) {
	e.throttleAvail.Set(b2f(ok))
	if ok {
		// Ряды заводятся сразу нулём. Иначе счётчик появлялся бы только с
		// ПЕРВЫМ событием троттлинга, и до него панель показывала бы «нет
		// данных» — то есть «не меряем», хотя меряем и результат нулевой.
		// Различать эти два состояния — весь смысл наблюдаемости стенда.
		e.throttleEvents.WithLabelValues("core").Add(0)
		e.throttleEvents.WithLabelValues("package").Add(0)
	}
}

// SetCPUFreqAvailable — отдаёт ли узел частоту ядер хоть каким-то способом.
func (e *Exporter) SetCPUFreqAvailable(ok bool) { e.freqAvail.Set(b2f(ok)) }

// SetCPUFreqSource фиксирует, откуда взята частота. Пустая строка — источника
// нет; тогда ряд не заводим вовсе, чтобы «нет источника» не выглядело как
// «источник cpufreq со значением 0».
func (e *Exporter) SetCPUFreqSource(source string) {
	if source == "" {
		return
	}
	e.freqSource.WithLabelValues(source).Set(1)
}

// AddThrottleEvents накапливает прирост счётчиков троттлинга. Counter с
// приростом, а не Gauge с сырым значением: sysfs отдаёт накопительное с
// момента загрузки узла, и первый тик приписал бы серии весь троттлинг за
// время жизни машины (дедупликация и прайминг — в pkg/cputhrottle).
func (e *Exporter) AddThrottleEvents(scope string, delta uint64) {
	if delta == 0 {
		return
	}
	e.throttleEvents.WithLabelValues(scope).Add(float64(delta))
}

// SetCPUFreq публикует частоту ядер узла (среднюю и минимальную).
func (e *Exporter) SetCPUFreq(avgHertz, minHertz, maxHertz float64) {
	e.cpuFreq.WithLabelValues("avg").Set(avgHertz)
	e.cpuFreq.WithLabelValues("min").Set(minHertz)
	e.cpuFreq.WithLabelValues("max").Set(maxHertz)
}

// AddRAPLJoules накапливает прирост энергии зоны. Counter, а не Gauge с сырым
// energy_uj: sysfs-счётчик переполняется (диапазон max_energy_range_uj), и
// коррекцию делает pkg/rapl — Prometheus видит уже монотонные джоули.
func (e *Exporter) AddRAPLJoules(zone, domain string, joules float64) {
	e.raplJoules.WithLabelValues(zone, domain).Add(joules)
}

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
