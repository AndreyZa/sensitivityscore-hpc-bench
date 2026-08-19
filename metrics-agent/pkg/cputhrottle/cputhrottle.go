// Package cputhrottle читает из sysfs два сигнала о том, что процессор
// измерительного узла работал не на полную: счётчики теплового троттлинга и
// текущую частоту ядер.
//
// Зачем это здесь, а не в node_exporter. На bench-узлах агент — единственный
// разрешённый постоянный процесс (k8s/monitoring/energy/README.md, вариант
// «а»), и добавление сюда не приводит на узел ничего нового. Тот же довод,
// что у pkg/rapl.
//
// Зачем вообще. Тепловой троттлинг УДЛИНЯЕТ задачу, ничего не сообщая: жертва
// честно досчитывает, харнесс пишет её runtime, и удлинение приписывается
// интерференции — то есть ровно тому, что серия измеряет. В логах причина не
// видна, в цифрах неотличима. Это тот же класс молчаливо неверных чисел, что
// CFS-троттлинг (закрыт метриками cAdvisor) и не-Guaranteed жертва (ловушка
// 18.08.2026), только источник другой и данных о нём не было совсем.
//
// Стоимость чтения — не одинаковая у двух сигналов, и это определяет их
// частоты:
//   - thermal_throttle/*_count живут в памяти ядра (их ведёт обработчик
//     теплового прерывания). Чтение бесплатно, снимаем каждый тик;
//   - scaling_cur_freq на intel_pstate читает MSR APERF/MPERF НА ЦЕЛЕВОМ
//     ядре. На 64-ядерном узле это десятки обращений к измеряемым ядрам, и
//     раз в 5 секунд их делать незачем: частота нужна как «насколько
//     просело», а факт просадки уже дают счётчики. Поэтому частота снимается
//     своим, редким циклом (CPU_FREQ_INTERVAL_SECONDS, по умолчанию 60 с).
//
// Дедупликация обязательна и не косметична. core_throttle_count одинаков у
// потоков одного ядра, package_throttle_count — у ВСЕХ ядер сокета. Наивная
// сумма по cpu* умножила бы пакетный счётчик на число ядер (на R760 — в 32
// раза) и выдала бы тепловую катастрофу на ровном месте.
package cputhrottle

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// cpu — один online-CPU с путями к тому, что у него удалось найти.
type cpu struct {
	id       int
	pkgID    int
	coreID   int
	corePath string // .../thermal_throttle/core_throttle_count, "" если нет
	pkgPath  string // .../thermal_throttle/package_throttle_count, "" если нет
	freqPath string // .../cpufreq/scaling_cur_freq, "" если нет
}

// Sampler держит найденные CPU и прошлые показания счётчиков троттлинга.
type Sampler struct {
	cpus []cpu
	// Представители: по одному CPU на (пакет, ядро) и по одному на пакет —
	// см. довод про дедупликацию в шапке пакета.
	coreReps []cpu
	pkgReps  []cpu

	lastCore uint64
	lastPkg  uint64
	primed   bool
}

// Counts — ПРИРОСТ счётчиков троттлинга с прошлого вызова.
type Counts struct {
	Core    uint64
	Package uint64
}

// Freq — частота ядер узла на момент опроса, в герцах.
type Freq struct {
	AvgHertz float64
	MinHertz float64
	CPUs     int
}

func readUint(path string) (uint64, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.ParseUint(strings.TrimSpace(string(raw)), 10, 64)
}

func readInt(path string) (int, error) {
	v, err := readUint(path)
	return int(v), err
}

func existing(path string) string {
	if _, err := os.Stat(path); err != nil {
		return ""
	}
	return path
}

// Discover перечисляет online-CPU под root (обычно /sys/devices/system/cpu; в
// контейнере — хостовый /sys через ro-mount, как у RAPL).
//
// Отсутствие thermal_throttle или cpufreq — не ошибка конфигурации, а
// свойство узла: в ВМ их нет, и вызывающий обязан честно погасить метрику,
// а не выдавать нули за измерение.
func Discover(root string) (*Sampler, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil, fmt.Errorf("cputhrottle: читаю %s: %w", root, err)
	}
	s := &Sampler{}
	for _, e := range entries {
		name := e.Name()
		if !strings.HasPrefix(name, "cpu") {
			continue
		}
		idPart := strings.TrimPrefix(name, "cpu")
		id, err := strconv.Atoi(idPart)
		if err != nil {
			continue // cpufreq, cpuidle, cpulist — не процессоры
		}
		dir := filepath.Join(root, name)
		// online отсутствует у cpu0 на многих ядрах — это не «offline».
		if raw, err := os.ReadFile(filepath.Join(dir, "online")); err == nil {
			if strings.TrimSpace(string(raw)) == "0" {
				continue
			}
		}
		c := cpu{id: id, pkgID: -1, coreID: -1}
		if v, err := readInt(filepath.Join(dir, "topology", "physical_package_id")); err == nil {
			c.pkgID = v
		}
		if v, err := readInt(filepath.Join(dir, "topology", "core_id")); err == nil {
			c.coreID = v
		}
		c.corePath = existing(filepath.Join(dir, "thermal_throttle", "core_throttle_count"))
		c.pkgPath = existing(filepath.Join(dir, "thermal_throttle", "package_throttle_count"))
		c.freqPath = existing(filepath.Join(dir, "cpufreq", "scaling_cur_freq"))
		s.cpus = append(s.cpus, c)
	}
	sort.Slice(s.cpus, func(i, j int) bool { return s.cpus[i].id < s.cpus[j].id })

	seenCore := map[[2]int]bool{}
	seenPkg := map[int]bool{}
	for _, c := range s.cpus {
		if c.corePath != "" {
			key := [2]int{c.pkgID, c.coreID}
			// Топология неизвестна (pkgID/coreID = -1) — считаем такой CPU
			// отдельным ядром: лучше посчитать дважды, чем пропустить узел
			// целиком. На реальных x86 топология есть всегда.
			if key == [2]int{-1, -1} {
				s.coreReps = append(s.coreReps, c)
			} else if !seenCore[key] {
				seenCore[key] = true
				s.coreReps = append(s.coreReps, c)
			}
		}
		if c.pkgPath != "" && !seenPkg[c.pkgID] {
			seenPkg[c.pkgID] = true
			s.pkgReps = append(s.pkgReps, c)
		}
	}
	return s, nil
}

// CPUs — сколько online-CPU нашлось.
func (s *Sampler) CPUs() int { return len(s.cpus) }

// ThrottleAvailable — есть ли на узле счётчики теплового троттлинга.
func (s *Sampler) ThrottleAvailable() bool { return len(s.coreReps) > 0 || len(s.pkgReps) > 0 }

// FreqAvailable — отдаёт ли узел текущую частоту ядер.
func (s *Sampler) FreqAvailable() bool {
	for _, c := range s.cpus {
		if c.freqPath != "" {
			return true
		}
	}
	return false
}

// SampleThrottle возвращает ПРИРОСТ счётчиков с прошлого вызова.
//
// Первый вызов приростом не считается: счётчики накопительные от загрузки
// узла, и отдать их целиком значило бы приписать текущей серии весь троттлинг
// за время жизни машины.
func (s *Sampler) SampleThrottle() (Counts, error) {
	var core, pkg uint64
	var firstErr error
	for _, c := range s.coreReps {
		v, err := readUint(c.corePath)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		core += v
	}
	for _, c := range s.pkgReps {
		v, err := readUint(c.pkgPath)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		pkg += v
	}
	var out Counts
	if s.primed {
		// Счётчики ядра монотонны; уменьшение возможно только при
		// перезагрузке узла (агент бы тоже перезапустился) или при уходе CPU
		// в offline. Отрицательный прирост не выдумываем — отдаём ноль.
		if core >= s.lastCore {
			out.Core = core - s.lastCore
		}
		if pkg >= s.lastPkg {
			out.Package = pkg - s.lastPkg
		}
	}
	s.lastCore, s.lastPkg, s.primed = core, pkg, true
	return out, firstErr
}

// SampleFreq возвращает среднюю и минимальную частоту online-ядер.
//
// Минимум важнее среднего: троттлинг обычно сажает часть ядер, и среднее по
// 64 ядрам это размывает.
func (s *Sampler) SampleFreq() (Freq, error) {
	var sum float64
	var min float64
	var n int
	var firstErr error
	for _, c := range s.cpus {
		if c.freqPath == "" {
			continue
		}
		v, err := readUint(c.freqPath)
		if err != nil {
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		hz := float64(v) * 1000 // sysfs отдаёт кГц
		sum += hz
		if n == 0 || hz < min {
			min = hz
		}
		n++
	}
	if n == 0 {
		return Freq{}, firstErr
	}
	return Freq{AvgHertz: sum / float64(n), MinHertz: min, CPUs: n}, firstErr
}
