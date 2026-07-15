"""Тесты смешанного (трёхвекторного) сценария: чередование профилей жертв
и нормализация штормов — чистые функции, кластера не требуют."""

from run_experiment import victim_profiles_for
from submit.aggressors import expected_pods, resolve_pressured_nodes, storm_specs

MIXED = {
    "name": "mixed3",
    "victims": [
        {"profile": "high-s", "count": 2},
        {"profile": "high-s-io", "count": 2},
        {"profile": "high-s-net", "count": 2},
    ],
    "storms": [
        {"node": "w8", "mode": "stress", "args": ["--stream", "2"], "per_node": 2,
         "toxic_for": ["high-s"]},
        {"node": "w9", "mode": "stress", "args": ["--hdd", "2"], "per_node": 2,
         "toxic_for": ["high-s-io"]},
        {"node": "w10", "mode": "net", "per_node": 2, "net_bitrate_mbps": 400,
         "toxic_for": ["high-s-net"]},
    ],
}

LEGACY = {"name": "io", "victim_profile": "high-s-io", "victim_count": 6,
          "aggressor_nodes": ["w10"], "aggressor_mode": None}


def test_victim_profiles_round_robin():
    # Профили чередуются, а не идут блоками — каждый равномерно размазан
    # по пуассоновскому окну прибытия.
    assert victim_profiles_for(MIXED) == [
        "high-s", "high-s-io", "high-s-net",
        "high-s", "high-s-io", "high-s-net",
    ]


def test_victim_profiles_uneven_counts():
    sc = {"victims": [{"profile": "a", "count": 3}, {"profile": "b", "count": 1}]}
    seq = victim_profiles_for(sc)
    assert sorted(seq) == ["a", "a", "a", "b"]
    assert seq[1] == "b"  # b не откладывается в хвост


def test_victim_profiles_legacy():
    assert victim_profiles_for(LEGACY) == ["high-s-io"] * 6


def test_storm_specs_none_for_legacy():
    assert storm_specs(LEGACY) is None


def test_storm_specs_normalized():
    specs = storm_specs(MIXED)
    assert [s["node"] for s in specs] == ["w8", "w9", "w10"]
    assert specs[2]["mode"] == "net" and specs[2]["net_bitrate_mbps"] == 400
    assert specs[0]["per_node"] == 2


def test_expected_pods_mixed():
    # 2 stress-шторма по 2 пода + net-шторм 2 пары (клиент+сервер) = 8.
    assert expected_pods(0, 0, MIXED) == 8


def test_expected_pods_legacy_net():
    assert expected_pods(1, 2, {"aggressor_mode": "net"}) == 4
    assert expected_pods(2, 3, {}) == 6


def test_resolve_pressured_nodes_mixed_all_stormed():
    # Смешанный сценарий давит все узлы — guard «нужна чистая нода»
    # не применяется, kubectl не дёргается.
    assert resolve_pressured_nodes(MIXED, {}) == ["w8", "w9", "w10"]


# --- net-egress: cross-node egress-шторм (насыщает uplink штормимой ноды) ---

NET_EGRESS = {
    "name": "net-egress-diff",
    "victims": [
        {"profile": "high-s-net", "count": 3},
        {"profile": "net-insensitive", "count": 3},
    ],
    "storms": [
        {"node": "w9", "mode": "net-egress", "per_node": 2,
         "egress_server_node": "w10", "parallel": 8, "toxic_for": ["high-s-net"]},
    ],
}


def test_storm_specs_net_egress_fields():
    specs = storm_specs(NET_EGRESS)
    assert len(specs) == 1
    s = specs[0]
    assert s["mode"] == "net-egress"
    assert s["egress_server_node"] == "w10"
    assert s["parallel"] == 8
    assert s["per_node"] == 2


def test_expected_pods_net_egress():
    # 2 клиента + 2 сервера (по одному на слот) = 4.
    assert expected_pods(0, 0, NET_EGRESS) == 4


def test_resolve_pressured_nodes_net_egress_only_storm_node():
    # Под давлением (по TX uplink) только штормимая нода; сервер-нода —
    # приёмник RX, жертвы могут планироваться туда без конфликта.
    assert resolve_pressured_nodes(NET_EGRESS, {}) == ["w9"]


def test_deploy_net_egress_requires_distinct_server(monkeypatch):
    # Сервер-нода обязана быть задана и отличаться от штормимой — иначе
    # egress не покидает ноду и uplink не насыщается.
    import submit.aggressors as agg
    import submit.k8s_submit as k8s_submit

    monkeypatch.setattr(k8s_submit, "ensure_namespace", lambda ns: None)
    monkeypatch.setattr(agg, "_apply_and_wait", lambda *a, **k: None)
    cfg = {"kubernetes": {"namespace": "bench"}, "images": {"aggressor": "img"},
           "aggressor": {}}

    bad = {**NET_EGRESS, "storms": [
        {"node": "w9", "mode": "net-egress", "per_node": 2,
         "egress_server_node": "w9"}]}  # тот же узел — ошибка
    import pytest
    with pytest.raises(RuntimeError, match="differ from the storm node"):
        agg.deploy([], 0, bad, cfg)


# --- Плацебо-сценарий: тот же поток жертв, ноль агрессоров -----------------

PLACEBO = {
    "name": "placebo",
    "victims": MIXED["victims"],
    "aggressors_per_node": [0],
    "pressured_node_count": 0,
}


def test_expected_pods_placebo_zero():
    assert expected_pods(0, 0, PLACEBO) == 0


def test_deploy_placebo_skips_kubectl(monkeypatch):
    # Нулевое давление: deploy обязан выйти ДО kubectl apply — пустой ввод
    # уронил бы плечо ('no objects passed to apply').
    import submit.aggressors as agg
    import submit.k8s_submit as k8s_submit

    monkeypatch.setattr(k8s_submit, "ensure_namespace", lambda ns: None)

    def _boom(*args, **kwargs):
        raise AssertionError("_apply_and_wait не должен вызываться без агрессоров")

    monkeypatch.setattr(agg, "_apply_and_wait", _boom)
    agg.deploy([], 0, PLACEBO, {"kubernetes": {"namespace": "bench"},
                                "images": {"aggressor": "img"}})
