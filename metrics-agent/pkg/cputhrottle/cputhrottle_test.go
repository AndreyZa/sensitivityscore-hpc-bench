package cputhrottle

import (
	"os"
	"path/filepath"
	"testing"
)

// fakeSysfs строит дерево вида /sys/devices/system/cpu под временным
// каталогом. pkgOf/coreOf задают топологию, чтобы проверить главное:
// пакетный счётчик НЕ должен умножаться на число ядер сокета.
func fakeSysfs(t *testing.T, cpus int, pkgOf, coreOf func(int) int,
	coreCount, pkgCount map[int]string, freq map[int]string) string {
	t.Helper()
	root := t.TempDir()
	for i := 0; i < cpus; i++ {
		dir := filepath.Join(root, "cpu"+itoa(i))
		topo := filepath.Join(dir, "topology")
		if err := os.MkdirAll(topo, 0o755); err != nil {
			t.Fatal(err)
		}
		write(t, filepath.Join(topo, "physical_package_id"), itoa(pkgOf(i)))
		write(t, filepath.Join(topo, "core_id"), itoa(coreOf(i)))
		if v, ok := coreCount[i]; ok {
			th := filepath.Join(dir, "thermal_throttle")
			os.MkdirAll(th, 0o755)
			write(t, filepath.Join(th, "core_throttle_count"), v)
		}
		if v, ok := pkgCount[i]; ok {
			th := filepath.Join(dir, "thermal_throttle")
			os.MkdirAll(th, 0o755)
			write(t, filepath.Join(th, "package_throttle_count"), v)
		}
		if v, ok := freq[i]; ok {
			cf := filepath.Join(dir, "cpufreq")
			os.MkdirAll(cf, 0o755)
			write(t, filepath.Join(cf, "scaling_cur_freq"), v)
		}
	}
	// Посторонние каталоги, которые лежат рядом в настоящем sysfs.
	os.MkdirAll(filepath.Join(root, "cpufreq"), 0o755)
	os.MkdirAll(filepath.Join(root, "cpuidle"), 0o755)
	return root
}

func write(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	neg := i < 0
	if neg {
		i = -i
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	if neg {
		return "-" + string(b)
	}
	return string(b)
}

func TestПакетныйСчётчикНеУмножаетсяНаЯдра(t *testing.T) {
	// Два сокета по четыре ядра. package_throttle_count лежит у КАЖДОГО cpu и
	// одинаков внутри сокета — наивная сумма дала бы 4x.
	core := map[int]string{}
	pkg := map[int]string{}
	for i := 0; i < 8; i++ {
		core[i] = "1" // по одному на ядро
		if i < 4 {
			pkg[i] = "10" // сокет 0
		} else {
			pkg[i] = "20" // сокет 1
		}
	}
	root := fakeSysfs(t, 8, func(i int) int { return i / 4 }, func(i int) int { return i % 4 },
		core, pkg, nil)

	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	if !s.ThrottleAvailable() {
		t.Fatal("счётчики троттлинга не найдены")
	}
	if _, err := s.SampleThrottle(); err != nil { // первый вызов — прайминг
		t.Fatal(err)
	}
	// Второй вызов без изменений: прирост обязан быть нулевым.
	got, err := s.SampleThrottle()
	if err != nil {
		t.Fatal(err)
	}
	if got.Core != 0 || got.Package != 0 {
		t.Fatalf("прирост без изменений: %+v, ожидался нулевой", got)
	}

	// Сокет 0 троттлит: +5. Ожидаем ровно 5, а не 5*4.
	for i := 0; i < 4; i++ {
		write(t, filepath.Join(root, "cpu"+itoa(i), "thermal_throttle", "package_throttle_count"), "15")
	}
	got, err = s.SampleThrottle()
	if err != nil {
		t.Fatal(err)
	}
	if got.Package != 5 {
		t.Fatalf("пакетный прирост = %d, ожидалось 5 (иначе счётчик умножен на число ядер)", got.Package)
	}
}

func TestПервыйВызовНеОтдаётИсториюОтЗагрузки(t *testing.T) {
	// Счётчики накопительные от загрузки узла: отдать их целиком значило бы
	// приписать серии весь троттлинг за время жизни машины.
	root := fakeSysfs(t, 2, func(int) int { return 0 }, func(i int) int { return i },
		map[int]string{0: "9999", 1: "9999"}, map[int]string{0: "777"}, nil)
	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	got, err := s.SampleThrottle()
	if err != nil {
		t.Fatal(err)
	}
	if got.Core != 0 || got.Package != 0 {
		t.Fatalf("первый вызов отдал %+v, ожидался нулевой прирост", got)
	}
}

func TestЧастотаСреднееИМинимум(t *testing.T) {
	root := fakeSysfs(t, 3, func(int) int { return 0 }, func(i int) int { return i },
		nil, nil, map[int]string{0: "3000000", 1: "2000000", 2: "1000000"})
	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	if !s.FreqAvailable() {
		t.Fatal("частота не найдена")
	}
	f, err := s.SampleFreq()
	if err != nil {
		t.Fatal(err)
	}
	if f.CPUs != 3 {
		t.Fatalf("CPUs = %d, ожидалось 3", f.CPUs)
	}
	if f.AvgHertz != 2e9 {
		t.Fatalf("среднее = %v, ожидалось 2e9", f.AvgHertz)
	}
	if f.MinHertz != 1e9 {
		t.Fatalf("минимум = %v, ожидалось 1e9 (троттлинг сажает ЧАСТЬ ядер, среднее это размывает)", f.MinHertz)
	}
}

func TestВМБезSysfsЭтоНеОшибка(t *testing.T) {
	// В ВМ (ss-system, STAGE) ни thermal_throttle, ни cpufreq нет. Агент обязан
	// честно погасить метрику, а не выдавать нули за измерение.
	root := fakeSysfs(t, 2, func(int) int { return 0 }, func(i int) int { return i }, nil, nil, nil)
	s, err := Discover(root)
	if err != nil {
		t.Fatalf("отсутствие счётчиков — свойство узла, а не ошибка: %v", err)
	}
	if s.ThrottleAvailable() || s.FreqAvailable() {
		t.Fatal("на узле без sysfs-счётчиков доступность обязана быть false")
	}
	if s.CPUs() != 2 {
		t.Fatalf("CPUs = %d, ожидалось 2", s.CPUs())
	}
}

func TestOfflineCPUПропускается(t *testing.T) {
	root := fakeSysfs(t, 2, func(int) int { return 0 }, func(i int) int { return i },
		map[int]string{0: "1", 1: "1"}, nil, nil)
	write(t, filepath.Join(root, "cpu1", "online"), "0")
	s, err := Discover(root)
	if err != nil {
		t.Fatal(err)
	}
	if s.CPUs() != 1 {
		t.Fatalf("CPUs = %d, ожидался 1: offline-CPU не считается", s.CPUs())
	}
}
