package perf

import "testing"

// Числа — из ground truth на wrk-b6 (SPR, 18.08.2026, perf bench mem memcpy
// под numactl; см. комментарий RemoteShare): события дизъюнктны, и прежний
// misses/loads на удалённом трафике давал тысячи (клампился в 1.0).
func TestRemoteShare(t *testing.T) {
	cases := []struct {
		name          string
		remote, local uint64
		want          float64
	}{
		{"локальная привязка (mem0)", 27_543, 11_535_640, 0.002},
		{"удалённая привязка (mem1)", 12_911_498, 4_695, 0.9996},
		{"полный 0/0 (пустое окно)", 0, 0, 0},
		{"только локальные", 0, 1000, 0},
		{"только удалённые", 1000, 0, 1},
	}
	for _, c := range cases {
		got := RemoteShare(c.remote, c.local)
		if got < 0 || got > 1 {
			t.Errorf("%s: RemoteShare=%v вне [0,1]", c.name, got)
		}
		if diff := got - c.want; diff > 0.01 || diff < -0.01 {
			t.Errorf("%s: RemoteShare=%v, ожидалось ~%v", c.name, got, c.want)
		}
	}

	// Прежняя формула на этих же числах — демонстрация насыщения: >>1.
	if r := Ratio(12_911_498, 4_695); r < 1000 {
		t.Errorf("санити: старый Ratio на удалённом трафике должен взрываться, получено %v", r)
	}
}

// Порог минимального DRAM-трафика (зонд 19.08.2026): кэш-резидентная жертва —
// ~7К событий/с при доле remote 0.97 — должна гейтиться в 0; настоящий
// удалённый трафик (миллионы событий/с) проходит без изменений.
func TestRemoteShareGated(t *testing.T) {
	const minRate = 100_000 // событий/с — дефолт агента

	// Зонд: 3 815 local + 107 911 remote за 15 с ≈ 7.4К/с — вырожденный режим.
	if got := RemoteShareGated(107_911, 3_815, 15, minRate); got != 0 {
		t.Errorf("кэш-резидентная жертва: ожидался гейт в 0, получено %v", got)
	}
	// Ground truth удалённой привязки, окно 1 с: ~12.9М/с — сильно выше порога.
	want := RemoteShare(12_911_498, 4_695)
	if got := RemoteShareGated(12_911_498, 4_695, 1, minRate); got != want {
		t.Errorf("настоящий remote-трафик: гейт исказил долю: %v != %v", got, want)
	}
	// Нулевое/отрицательное окно — честный 0, не деление на ноль.
	if got := RemoteShareGated(1000, 1000, 0, minRate); got != 0 {
		t.Errorf("elapsed=0: ожидался 0, получено %v", got)
	}
	// Порог 0 — гейт выключен, поведение эквивалентно RemoteShare.
	if got := RemoteShareGated(107_911, 3_815, 15, 0); got != RemoteShare(107_911, 3_815) {
		t.Errorf("порог 0 должен отключать гейт, получено %v", got)
	}
}
