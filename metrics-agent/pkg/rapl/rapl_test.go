package rapl

import (
	"os"
	"path/filepath"
	"testing"
)

// фальшивое powercap-дерево: пишем файлы так, как их раскладывает ядро.
func writeZone(t *testing.T, root, id, name string, energyUJ, maxRangeUJ uint64) {
	t.Helper()
	dir := filepath.Join(root, id)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	files := map[string]uint64{"energy_uj": energyUJ}
	if maxRangeUJ > 0 {
		files["max_energy_range_uj"] = maxRangeUJ
	}
	if err := os.WriteFile(filepath.Join(dir, "name"), []byte(name+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for f, v := range files {
		if err := os.WriteFile(filepath.Join(dir, f), []byte(uintStr(v)+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}

func uintStr(v uint64) string {
	b := []byte{}
	if v == 0 {
		return "0"
	}
	for v > 0 {
		b = append([]byte{byte('0' + v%10)}, b...)
		v /= 10
	}
	return string(b)
}

func setEnergy(t *testing.T, root, id string, energyUJ uint64) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(root, id, "energy_uj"), []byte(uintStr(energyUJ)+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestDiscoverSkipsTypeDirAndMMIO(t *testing.T) {
	root := t.TempDir()
	// каталог типа — есть name (у ядра enabled/uevent, но не energy_uj)
	if err := os.MkdirAll(filepath.Join(root, "intel-rapl"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeZone(t, root, "intel-rapl:0", "package-0", 1000, 2_000_000)
	writeZone(t, root, "intel-rapl:0:0", "dram", 500, 2_000_000)
	writeZone(t, root, "intel-rapl-mmio:0", "package-0", 999, 2_000_000) // дубль по другой шине

	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	if s.Zones() != 2 {
		t.Fatalf("Zones() = %d, ожидалось 2 (тип и mmio исключены): %v", s.Zones(), s.IDs())
	}
}

func TestDiscoverMissingRootIsNotAnError(t *testing.T) {
	s, err := Discover(filepath.Join(t.TempDir(), "нет-такого"))
	if err != nil {
		t.Fatalf("отсутствие powercap (ВМ) не должно быть ошибкой: %v", err)
	}
	if s.Zones() != 0 {
		t.Fatalf("Zones() = %d, ожидалось 0", s.Zones())
	}
}

func TestSampleDeltaAndWraparound(t *testing.T) {
	root := t.TempDir()
	writeZone(t, root, "intel-rapl:0", "package-0", 1_000_000, 10_000_000) // 1 Дж, диапазон 10 Дж
	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}

	// обычный прирост: 1 Дж -> 3.5 Дж => дельта 2.5 Дж
	setEnergy(t, root, "intel-rapl:0", 3_500_000)
	d, err := s.Sample()
	if err != nil {
		t.Fatal(err)
	}
	if len(d) != 1 || d[0].Joules != 2.5 {
		t.Fatalf("дельта = %+v, ожидалось 2.5 Дж", d)
	}

	// переполнение: 3.5 Дж -> 0.5 Дж при диапазоне 10 => 10-3.5+0.5 = 7 Дж
	setEnergy(t, root, "intel-rapl:0", 500_000)
	d, err = s.Sample()
	if err != nil {
		t.Fatal(err)
	}
	if len(d) != 1 || d[0].Joules != 7.0 {
		t.Fatalf("дельта после wrap = %+v, ожидалось 7.0 Дж", d)
	}
}

func TestSampleBackwardsWithoutRangeSkipsZone(t *testing.T) {
	root := t.TempDir()
	writeZone(t, root, "intel-rapl:1", "psys", 5_000_000, 0) // диапазон неизвестен
	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	setEnergy(t, root, "intel-rapl:1", 1_000_000)
	d, err := s.Sample()
	if err == nil {
		t.Fatal("счётчик назад без диапазона обязан вернуть ошибку (сброс не маскируем)")
	}
	if len(d) != 0 {
		t.Fatalf("зона со сбросом не должна дать дельту: %+v", d)
	}
	// после сброса счёт продолжается от нового значения
	setEnergy(t, root, "intel-rapl:1", 2_000_000)
	d, err = s.Sample()
	if err != nil {
		t.Fatal(err)
	}
	if len(d) != 1 || d[0].Joules != 1.0 {
		t.Fatalf("дельта после восстановления = %+v, ожидалось 1.0 Дж", d)
	}
}
