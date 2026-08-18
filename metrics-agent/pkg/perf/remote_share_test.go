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
