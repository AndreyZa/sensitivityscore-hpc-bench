// Package rapl читает накопительные счётчики энергии Intel RAPL из
// powercap-sysfs и отдаёт их как монотонные джоули с коррекцией переполнения.
//
// Зачем это в агенте, а не в node_exporter: на измерительных узлах агент —
// единственный разрешённый постоянный процесс (k8s/monitoring/energy/README.md,
// вариант «а»), и добавление RAPL сюда не приводит ни одного нового процесса
// на bench. Чистоту измерений это не трогает: чтение energy_uj — микросекундный
// MSR-backed sysfs-read раз в тик (6 файлов / 5 с), PMU-счётчики не участвуют
// вовсе — прогноз A5 (доля окна 1.000) этим не задевается.
//
// Точка правды методики — РАЗНОСТЬ накопительного счётчика на границах окна
// (scripts/energy-window.py, миграция 003): поэтому наружу идёт прирост в
// джоулях (Prometheus counter), а не мгновенная мощность. Переполнение
// energy_uj (диапазон max_energy_range_uj: у package ~262 кДж — часы под
// нагрузкой, у dram меньше) корректируется по известному диапазону; сброс без
// известного диапазона не маскируется — зона пропускает тик с ошибкой.
package rapl

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Zone — одна powercap-зона (package-0, dram, psys, ...). Имена доменов
// дублируются между сокетами (dram есть у intel-rapl:0:0 и intel-rapl:2:0),
// поэтому идентичность зоны задаёт ID — basename каталога в sysfs.
type Zone struct {
	dir      string
	ID       string // intel-rapl:0, intel-rapl:0:0, ...
	Name     string // package-0, dram, psys, ...
	maxRange uint64 // max_energy_range_uj; 0 = неизвестен, wrap не скорректировать
	last     uint64 // прошлое показание energy_uj
}

// Delta — прирост энергии зоны с прошлого Sample, в джоулях.
type Delta struct {
	ID     string
	Name   string
	Joules float64
}

// Sampler держит найденные зоны и их последние показания.
type Sampler struct {
	zones []*Zone
}

// Discover перечисляет зоны в root (обычно /sys/class/powercap; в контейнере —
// хостовый /sys через ro-mount: runc после PLATYPUS маскирует контейнерный
// /sys/devices/virtual/powercap, а energy_uj читается только root'ом).
// Зона = подкаталог с читаемыми name и energy_uj; служебные каталоги типов
// (intel-rapl без счётчика) отпадают сами. Зоны intel-rapl-mmio:* исключаются
// явно: это те же package-счётчики через другую шину, и Σ RAPL в кросс-сверке
// Э0.2 (предрегистрация энергопрогноза) не должна считать энергию дважды.
//
// Отсутствие root или пустой список зон — не ошибка конфигурации, а свойство
// узла (ВМ, STAGE): вызывающий смотрит на Zones() и честно гасит метрику.
func Discover(root string) (*Sampler, error) {
	entries, err := os.ReadDir(root)
	if err != nil {
		if os.IsNotExist(err) {
			return &Sampler{}, nil
		}
		return nil, fmt.Errorf("rapl root %s: %w", root, err)
	}
	s := &Sampler{}
	for _, e := range entries {
		id := e.Name()
		if strings.HasPrefix(id, "intel-rapl-mmio") {
			continue
		}
		dir := filepath.Join(root, id)
		name, err := os.ReadFile(filepath.Join(dir, "name"))
		if err != nil {
			continue // каталог типа или чужой класс — не зона
		}
		cur, err := readUint(filepath.Join(dir, "energy_uj"))
		if err != nil {
			continue // без счётчика зона бесполезна (или нет прав — см. пакетный комментарий)
		}
		// Диапазон счётчика: без него переполнение не отличить от сброса.
		maxRange, _ := readUint(filepath.Join(dir, "max_energy_range_uj"))
		s.zones = append(s.zones, &Zone{
			dir:      dir,
			ID:       id,
			Name:     strings.TrimSpace(string(name)),
			maxRange: maxRange,
			last:     cur,
		})
	}
	return s, nil
}

// Zones — сколько зон реально читается. 0 = RAPL на узле нет (ВМ) или нет прав.
func (s *Sampler) Zones() int { return len(s.zones) }

// IDs — список зон вида "intel-rapl:0=package-0" для стартового лога.
func (s *Sampler) IDs() []string {
	out := make([]string, 0, len(s.zones))
	for _, z := range s.zones {
		out = append(out, z.ID+"="+z.Name)
	}
	return out
}

// Sample возвращает прирост джоулей по зонам с прошлого вызова (первый вызов —
// с момента Discover). Ошибка одной зоны не роняет остальные: зона пропускает
// тик, ошибки склеиваются в возврат — вызывающий логирует, счётчик не растёт
// (недосчитать честнее, чем дописать выдуманное).
func (s *Sampler) Sample() ([]Delta, error) {
	var deltas []Delta
	var errs []string
	for _, z := range s.zones {
		cur, err := readUint(filepath.Join(z.dir, "energy_uj"))
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s: %v", z.ID, err))
			continue
		}
		var duj uint64
		switch {
		case cur >= z.last:
			duj = cur - z.last
		case z.maxRange > 0:
			// Переполнение: счётчик прошёл через max_energy_range_uj.
			duj = z.maxRange - z.last + cur
		default:
			errs = append(errs, fmt.Sprintf("%s: счётчик пошёл назад (%d -> %d), диапазон неизвестен — тик зоны пропущен", z.ID, z.last, cur))
			z.last = cur
			continue
		}
		z.last = cur
		deltas = append(deltas, Delta{ID: z.ID, Name: z.Name, Joules: float64(duj) / 1e6})
	}
	if len(errs) > 0 {
		return deltas, fmt.Errorf("%s", strings.Join(errs, "; "))
	}
	return deltas, nil
}

func readUint(path string) (uint64, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return 0, err
	}
	return strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
}
