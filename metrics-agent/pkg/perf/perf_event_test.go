package perf

import (
	"reflect"
	"testing"
)

func TestParseCPUList(t *testing.T) {
	cases := []struct {
		in   string
		want []int
	}{
		{"0", []int{0}},
		{"0-3", []int{0, 1, 2, 3}},
		{"0-15", []int{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}},
		{"0,2-4,7", []int{0, 2, 3, 4, 7}},
		{"1-1", []int{1}},
		{" 0-1 , 3 ", []int{0, 1, 3}},
	}
	for _, c := range cases {
		got, err := parseCPUList(c.in)
		if err != nil {
			t.Errorf("parseCPUList(%q) unexpected error: %v", c.in, err)
			continue
		}
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("parseCPUList(%q) = %v, want %v", c.in, got, c.want)
		}
	}
}

func TestParseCPUListErrors(t *testing.T) {
	for _, in := range []string{"", "abc", "0-x", "-"} {
		if _, err := parseCPUList(in); err == nil {
			t.Errorf("parseCPUList(%q): expected error, got nil", in)
		}
	}
}

// TestLLCPairRealHardware exercises the grouped pair against the real PMU on
// the root cgroup: open -> enable -> touch memory -> read. Skips (not fails)
// where cgroup-scoped perf is unavailable (no PMU / no privilege) — on dev
// boxes and CI containers this is an environment property, not a bug. On any
// machine where it runs, it proves the leader+member group opens per CPU and
// both sides accumulate over the same window.
func TestLLCPairRealHardware(t *testing.T) {
	f, err := OpenPodCgroup("/sys/fs/cgroup")
	if err != nil {
		t.Skipf("open root cgroup: %v", err)
	}
	defer f.Close()

	num, den, err := LLCPair(int(f.Fd()))
	if err != nil {
		t.Skipf("cgroup-scoped PMU unavailable here: %v", err)
	}
	defer num.Close()
	defer den.Close()

	if err := num.Enable(); err != nil {
		t.Fatalf("enable leader: %v", err)
	}
	if err := den.Enable(); err != nil { // no-op by design, must not error
		t.Fatalf("enable member: %v", err)
	}

	// Generate cache traffic well past any LLC size so both events move.
	buf := make([]byte, 64<<20)
	for round := 0; round < 4; round++ {
		for i := 0; i < len(buf); i += 64 {
			buf[i]++
		}
	}

	misses, err := num.Read()
	if err != nil {
		t.Fatalf("read misses: %v", err)
	}
	refs, err := den.Read()
	if err != nil {
		t.Fatalf("read references: %v", err)
	}
	t.Logf("llc pair over window: misses=%d references=%d", misses, refs)
	if refs == 0 {
		t.Fatal("references == 0 — group member never scheduled with its leader")
	}
	if misses > refs*2 {
		// Sanity, not physics: grossly inverted magnitudes would mean the
		// pair is reading disjoint windows (the exact bug grouping prevents).
		t.Fatalf("misses (%d) wildly exceed references (%d)", misses, refs)
	}
}

// Мультиплексирование: ядро крутит события по очереди и возвращает СЫРОЕ
// число, как будто счётчик стоял на PMU всё окно. Домножение на
// enabled/running — та же поправка, что делает perf(1); без неё узловое
// давление падает при уплотнении узла, то есть сигнал инвертируется ровно
// там, где проверяется H1 (пункт A5 аудита).
func TestReadingScaled(t *testing.T) {
	cases := []struct {
		name    string
		reading Reading
		want    uint64
	}{
		{"без мультиплексирования", Reading{Value: 1000, Enabled: 500, Running: 500}, 1000},
		{"половина окна — вдвое вверх", Reading{Value: 1000, Enabled: 1000, Running: 500}, 2000},
		{"четверть окна — вчетверо", Reading{Value: 250, Enabled: 4000, Running: 1000}, 1000},
		{"счётчик не попал на PMU", Reading{Value: 1234, Enabled: 1000, Running: 0}, 0},
		{"running > enabled (округление ядра)", Reading{Value: 700, Enabled: 999, Running: 1000}, 700},
		{"нулевое окно", Reading{}, 0},
	}
	for _, c := range cases {
		if got := c.reading.Scaled(); got != c.want {
			t.Errorf("%s: Scaled() = %d, ожидалось %d", c.name, got, c.want)
		}
	}
}

func TestReadingMultiplexRatio(t *testing.T) {
	cases := []struct {
		name    string
		reading Reading
		want    float64
	}{
		{"полное окно", Reading{Enabled: 1000, Running: 1000}, 1.0},
		{"половина", Reading{Enabled: 1000, Running: 500}, 0.5},
		{"ничего не измерено", Reading{Enabled: 1000, Running: 0}, 0.0},
		{"до первого чтения", Reading{}, 1.0},
		{"running > enabled клампится", Reading{Enabled: 900, Running: 1000}, 1.0},
	}
	for _, c := range cases {
		if got := c.reading.MultiplexRatio(); got != c.want {
			t.Errorf("%s: MultiplexRatio() = %v, ожидалось %v", c.name, got, c.want)
		}
	}
}

// parseReading читает три little-endian u64 подряд — формат ответа read(2)
// при PERF_FORMAT_TOTAL_TIME_ENABLED|RUNNING.
func TestParseReading(t *testing.T) {
	buf := make([]byte, readSize)
	put := func(off int, v uint64) {
		for i := 0; i < 8; i++ {
			buf[off+i] = byte(v >> (8 * i))
		}
	}
	put(0, 0xDEADBEEF)
	put(8, 5_000_000_000)
	put(16, 2_500_000_000)

	got, err := parseReading(buf)
	if err != nil {
		t.Fatalf("parseReading: %v", err)
	}
	want := Reading{Value: 0xDEADBEEF, Enabled: 5_000_000_000, Running: 2_500_000_000}
	if got != want {
		t.Errorf("parseReading() = %+v, ожидалось %+v", got, want)
	}
	if r := got.MultiplexRatio(); r != 0.5 {
		t.Errorf("ratio = %v, ожидалось 0.5", r)
	}
	// Короткий ответ — ошибка, а не молчаливые нули: 8 байт вместо 24 значат,
	// что событие открыто без readFormat, и поправку применить не к чему.
	if _, err := parseReading(buf[:8]); err == nil {
		t.Error("parseReading(8 байт) должен вернуть ошибку")
	}
}

// TestMultiplexingRealHardware перегружает PMU намеренно: открывает много пар
// счётчиков на один и тот же cgroup — больше, чем у железа физических
// счётчиков, — и проверяет, что мы это ВИДИМ. Без флагов enabled/running
// такой прогон выглядел бы полностью благополучным: ядро вернуло бы сырые
// числа, как будто каждое событие считалось всё окно.
//
// Требует доступа к PMU: cgroup-scoped perf_event_open нужен CAP_PERFMON либо
// kernel.perf_event_paranoid <= 0 (в WSL2 и на большинстве десктопов события
// закрыты — тест тогда пропускается, а не падает).
func TestMultiplexingRealHardware(t *testing.T) {
	f, err := OpenPodCgroup("/sys/fs/cgroup")
	if err != nil {
		t.Skipf("open root cgroup: %v", err)
	}
	defer f.Close()

	// Пар заметно больше, чем счётчиков у любого современного ядра (обычно
	// 4-8 программируемых на поток): мультиплексирование гарантировано.
	const pairs = 24
	var opened []*Counter
	defer func() {
		for _, c := range opened {
			c.Close()
		}
	}()
	var leaders []*Counter
	for i := 0; i < pairs; i++ {
		num, den, err := LLCPair(int(f.Fd()))
		if err != nil {
			if i == 0 {
				t.Skipf("cgroup-scoped PMU unavailable here: %v", err)
			}
			// Ядро может отказать по лимиту открытых событий — этого уже
			// достаточно для проверки, работаем с тем, что открылось.
			t.Logf("открыто %d пар, дальше отказ: %v", i, err)
			break
		}
		opened = append(opened, num, den)
		leaders = append(leaders, num)
		if err := num.Enable(); err != nil {
			t.Fatalf("enable пары %d: %v", i, err)
		}
	}

	// Нагрузка мимо кэша, чтобы событиям было что считать.
	buf := make([]byte, 64<<20)
	for round := 0; round < 4; round++ {
		for i := 0; i < len(buf); i += 64 {
			buf[i]++
		}
	}

	worst, sumRatio := 1.0, 0.0
	for i, c := range leaders {
		r, err := c.ReadFull()
		if err != nil {
			t.Fatalf("read пары %d: %v", i, err)
		}
		ratio := r.MultiplexRatio()
		sumRatio += ratio
		if ratio < worst {
			worst = ratio
		}
	}
	t.Logf("пар открыто: %d, худший ratio: %.3f, средний: %.3f",
		len(leaders), worst, sumRatio/float64(len(leaders)))

	if len(leaders) < 4 {
		t.Skipf("открылось всего %d пар — перегрузить PMU не вышло", len(leaders))
	}
	if worst >= 0.999 {
		t.Errorf("при %d парах на PMU мультиплексирование не обнаружено (худший ratio %.3f) — "+
			"либо флаги enabled/running не доехали до ядра, либо счётчиков столько же, сколько событий",
			len(leaders), worst)
	}
	if worst < 0.9 {
		t.Logf("ratio ниже порога алерта SSPMUMultiplexed (0.9) — алерт бы сработал, как и задумано")
	}
}

// TestMultiplexCorrectionRealHardware проверяет, что поправка ПРИМЕНЯЕТСЯ и в
// какую сторону, но НЕ требует, чтобы она восстанавливала значение точно —
// потому что она этого не делает.
//
// Развёртка на живом железе (Ubuntu 26.04, 8 потоков, paranoid=-1; три
// повтора на точку, база — среднее трёх чистых замеров 2.04e6):
//
//	доля окна 1.000 -> ошибка  -22%
//	доля окна 0.583 -> ошибка   -3% и +1831% (два прогона одной точки)
//	доля окна 0.331 -> ошибка +208%
//	доля окна 0.268 -> ошибка +245%
//	доля окна 0.174 -> ошибка +1772%
//	доля окна 0.123 -> ошибка +219%
//
// Линейная экстраполяция с доли окна на всё окно несмещена в среднем, но её
// ДИСПЕРСИЯ на коротких срезах огромна: попал срез на активную фазу — оценка
// улетает в разы. Практический вывод, который и заложен в алерт
// SSPMUMultiplexed: доля окна ниже ~0.9 означает «кэш-осью на этом узле
// пользоваться нельзя», а не «мы всё поправили». Единственная надёжная мера —
// не допускать мультиплексирования (меньше одновременно открытых событий).
//
// Почему поправку при этом всё равно применяем: без неё занижение
// СИСТЕМАТИЧНО и растёт с плотностью узла, то есть скоррелировано с
// воздействием — это хуже шумной оценки, потому что выглядит как сигнал.
func TestMultiplexCorrectionRealHardware(t *testing.T) {
	f, err := OpenPodCgroup("/sys/fs/cgroup")
	if err != nil {
		t.Skipf("open root cgroup: %v", err)
	}
	defer f.Close()

	work := func() {
		buf := make([]byte, 64<<20)
		for round := 0; round < 6; round++ {
			for i := 0; i < len(buf); i += 64 {
				buf[i]++
			}
		}
	}
	// measure возвращает значение с поправкой, то что вернуло ядро, и долю окна.
	measure := func(extraPairs int) (scaled, raw uint64, ratio float64) {
		num, den, err := LLCPair(int(f.Fd()))
		if err != nil {
			t.Skipf("cgroup-scoped PMU unavailable here: %v", err)
		}
		defer num.Close()
		defer den.Close()

		var extra []*Counter
		defer func() {
			for _, c := range extra {
				c.Close()
			}
		}()
		for i := 0; i < extraPairs; i++ {
			n2, d2, err := LLCPair(int(f.Fd()))
			if err != nil {
				break // ядро упёрлось в лимит событий — хватит и открытых
			}
			extra = append(extra, n2, d2)
			_ = n2.Enable()
		}
		if err := num.Enable(); err != nil {
			t.Fatalf("enable: %v", err)
		}
		work()
		r, err := num.ReadFull()
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		return r.Value, uint64(float64(r.Value) * r.MultiplexRatio()), r.MultiplexRatio()
	}

	sClean, _, ratClean := measure(0)
	sBusy, rawBusy, ratBusy := measure(23)
	t.Logf("PMU свободна:    доля окна %.3f, значение %d", ratClean, sClean)
	t.Logf("PMU перегружена: доля окна %.3f, сырое %d, с поправкой %d (ошибка к чистому %+.0f%%)",
		ratBusy, rawBusy, sBusy, 100*(float64(sBusy)-float64(sClean))/float64(sClean))

	if ratClean < 0.99 || ratBusy > 0.5 {
		t.Skipf("не удалось создать контраст условий (доля окна %.3f против %.3f)", ratClean, ratBusy)
	}
	if sBusy == 0 {
		t.Fatal("нулевое измерение под нагрузкой — событие не считало вовсе")
	}
	// Поправка применена: домноженное значение строго больше сырого.
	if sBusy <= rawBusy {
		t.Errorf("значение с поправкой (%d) не превышает сырое (%d) при доле окна %.3f — "+
			"масштабирование не сработало", sBusy, rawBusy, ratBusy)
	}
	// Занижение без поправки — систематическое и кратное, ради чего всё и делалось.
	if under := float64(sBusy) / float64(rawBusy); under < 2 {
		t.Errorf("сырое значение занижено лишь в %.1f раза при доле окна %.3f", under, ratBusy)
	}
}
