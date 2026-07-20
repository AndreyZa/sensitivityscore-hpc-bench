"""Сводки из файлов данных: results/baselines parquet и comparisons.csv
анализа. pandas импортируется лениво — страница поднимается и без него,
секции данных честно покажут ошибку."""

from __future__ import annotations

import time
from pathlib import Path

from .progress import expected_by_scenario


def storm_nodes_by_scenario(cfg: dict) -> dict[str, set]:
    """scenario-колонка (pressure:<name>) -> множество перегружаемых узлов
    (легаси aggressor_nodes либо узлы storms смешанного сценария)."""
    out = {}
    for sc in cfg.get("pressure_scenarios", []):
        if "storms" in sc:
            out[f"pressure:{sc['name']}"] = {s["node"] for s in sc["storms"]}
        else:
            out[f"pressure:{sc['name']}"] = set(sc.get("aggressor_nodes") or [])
    return out


AXES = ("llc", "numa", "net", "io")
AXIS_LABEL = {"llc": "кэш", "numa": "память", "net": "сеть", "io": "диск"}


def costly_axis(cfg: dict) -> str | None:
    """Самая дорогая ось по КАЛИБРОВКЕ (score_weights.base) — та, чей шторм
    реально бьёт по любой задаче узла.

    Зачем это счётчику. В смешанном сценарии перегружены ВСЕ узлы, поэтому
    «попал в шторм» само по себе ничего не значит: на этом стенде кэш-шторм
    при квотах 500m бесплатен (base llc = 0), сеть почти бесплатна (0.09), а
    дисковый бьёт всех (1.0). Считать «совпадения с объявленной осью» — значит
    записывать в ошибки намеренный и правильный выбор дешёвого узла: счётчик
    получался одинаковым у всех планировщиков и ничего не различал. Дорогой
    узел — единственный, попадание на который действительно платное.

    None, если весов нет или все базовые цены равны (различать нечего)."""
    weights = cfg.get("score_weights") or {}
    base = weights.get("base") if isinstance(weights, dict) else None
    if not isinstance(base, dict):
        return None
    costs = {a: float(base.get(a, 0.0) or 0.0) for a in AXES}
    top = max(costs, key=lambda a: costs[a])
    others = [c for a, c in costs.items() if a != top]
    if costs[top] <= 0 or all(c == costs[top] for c in others):
        return None
    return top


# Зеркало extractSensitivityVector плагина (и SENSITIVITY_VALUE харнесса):
# в строках результата чувствительность лежит СЛОВОМ, а не числом.
SENSITIVITY_VALUE = {"high": 1.0, "medium": 0.5}


def profiles_sensitive_to(df, axis: str) -> set[str]:
    """Профили, ОБЪЯВЛЕННЫЕ чувствительными по оси axis — прямо из строк
    результата (колонка sensitivity_<ось>, то самое, что видел планировщик).

    Берётся из данных, а не из harness/profiles.py: страница живёт в
    контейнере, где модулей харнесса нет. Спрашивается именно «чувствителен
    ли профиль к ЭТОЙ оси», а не «какая ось у него главная»: у профиля
    high-s-io high стоит сразу на llc, numa и io, и «главная» ось из этого не
    выводится — а вот принадлежность к io однозначна."""
    col = f"sensitivity_{axis}"
    if col not in df or "profile" not in df:
        return set()
    hot = df[df[col].astype(str).str.lower().map(SENSITIVITY_VALUE).fillna(0.0) > 0]
    return {str(p) for p in hot["profile"].unique()}


def costly_nodes_by_scenario(cfg: dict, costly_profiles: set[str]) -> dict[str, set]:
    """scenario-колонка -> узлы, чей шторм бьёт по самой дорогой оси.

    Ось шторма выводится через toxic_for: шторм объявлен токсичным для
    профилей, а профиль — чувствительным по осям. Так конфигу не нужно
    отдельное поле «ось шторма», и разметка остаётся одна."""
    if not costly_profiles:
        return {}
    out: dict[str, set] = {}
    for sc in cfg.get("pressure_scenarios", []):
        nodes = {
            s["node"]
            for s in sc.get("storms", []) or []
            if costly_profiles & set(s.get("toxic_for") or [])
        }
        if nodes:
            out[f"pressure:{sc['name']}"] = nodes
    return out


def toxic_map_by_scenario(cfg: dict) -> dict[str, dict[str, set]]:
    """Для смешанных сценариев: scenario-колонка -> {профиль: множество
    «своих токсичных» узлов} из storms[].toxic_for. В смешанном сценарии
    перегружены ВСЕ узлы, и качество решения — не «избежал шторма вообще»,
    а «не поставил задачу на шторм её собственной оси»."""
    out: dict[str, dict[str, set]] = {}
    for sc in cfg.get("pressure_scenarios", []):
        m: dict[str, set] = {}
        for s in sc.get("storms", []) or []:
            for prof in s.get("toxic_for", []) or []:
                m.setdefault(prof, set()).add(s["node"])
        if m:
            out[f"pressure:{sc['name']}"] = m
    return out


def pressure_results(path: Path, cfg: dict) -> dict:
    """Ключевая метрика по results.parquet: на каждый сценарий и планировщик —
    задач всего, сколько размещено на перегруженный узел, среднее время
    выполнения и ошибка размещения. Прямой ответ «уводит ли планировщик задачи
    от фоновой нагрузки» — считается инкрементально по мере прогона."""
    if not path.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out: dict = {
            "exists": True,
            "rows": len(df),
            "mtime": time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime)),
        }
        if "approximation" in df:
            out["error_rows"] = int(
                df["approximation"].astype(str).str.startswith("error:").sum()
            )
        storm = storm_nodes_by_scenario(cfg)
        toxic = toxic_map_by_scenario(cfg)
        axis = costly_axis(cfg)
        costly = costly_nodes_by_scenario(
            cfg, profiles_sensitive_to(df, axis) if axis else set()
        )
        costly_label = AXIS_LABEL.get(axis or "", "")
        exp_sc = expected_by_scenario(cfg)
        scenarios: dict = {}
        if "scenario" in df and "config" in df:
            dfp = df[df["scenario"].astype(str).str.startswith("pressure:")]
            for sc, g in dfp.groupby("scenario"):
                storm_nodes = storm.get(sc, set())
                costly_nodes = costly.get(sc, set())
                # Смешанный сценарий с калибровкой: считаем попадания на
                # ДОРОГОЙ узел. Номинальное совпадение с объявленной осью
                # (toxic_for) для этого не годится — см. costly_axis.
                tox = toxic.get(sc) if not costly_nodes else None
                arms: dict = {}
                for arm, ga in g.groupby("config"):
                    bad = (
                        ga["approximation"].astype(str).str.startswith("error:")
                        if "approximation" in ga
                        else pd.Series(False, index=ga.index)
                    )
                    ok = ga[ga["makespan_s"].notna() & ~bad]
                    if costly_nodes:
                        # Есть калибровка цен осей: «в шторм» = на дорогой узел.
                        in_storm = int(ok["node"].isin(costly_nodes).sum()) if len(ok) else 0
                    elif tox:
                        # Смешанный сценарий без калибровки: «в шторм» = на
                        # узел, токсичный именно для профиля этой задачи.
                        in_storm = int(sum(
                            r.node in tox.get(r.profile, set())
                            for r in ok.itertuples()
                        )) if len(ok) else 0
                    else:
                        in_storm = int(ok["node"].isin(storm_nodes).sum()) if len(ok) else 0
                    reg = ok["placement_regret"].dropna() if "placement_regret" in ok else []
                    arms[arm] = {
                        "victims": int(len(ga)),
                        "measured": int(len(ok)),
                        "storm": in_storm,
                        "storm_pct": round(100 * in_storm / len(ok)) if len(ok) else None,
                        "makespan": round(float(ok["makespan_s"].mean()), 1) if len(ok) else None,
                        "regret": round(float(reg.mean()), 3) if len(reg) else None,
                    }
                scenarios[sc] = {
                    "storm_node": ", ".join(sorted(storm_nodes)) or "?",
                    "expected": exp_sc.get(sc),
                    "done": int(len(g)),
                    "arms": arms,
                    # Счётчик «в шторм» — НОМИНАЛЬНОЕ совпадение с
                    # декларированной осью: различает плохо, потому что узел
                    # дешёвой оси занимать не ошибка. Остаётся только там, где
                    # цены осей не откалиброваны (см. costly_axis).
                    "nominal": bool(tox),
                    # Дорогой узел найден — счётчик стал содержательным:
                    # попадание на него платное для любой задачи.
                    "costly_axis": costly_label if costly_nodes else "",
                    "costly_node": ", ".join(sorted(costly_nodes)),
                }
        out["scenarios"] = scenarios
        return out
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}


def baseline_summary(path: Path) -> dict:
    """Эталонные прогоны как матрица профиль × узел (медианное время) — сразу
    видна аппаратная неоднородность узлов."""
    if not path.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_parquet(path)
        out: dict = {
            "exists": True,
            "rows": len(df),
            "mtime": time.strftime("%H:%M:%S", time.localtime(path.stat().st_mtime)),
        }
        solo = df[df["scenario"] == "baseline"] if "scenario" in df else df
        solo = solo[solo["makespan_s"].notna()]
        if len(solo) and "node" in solo and "profile" in solo:
            nodes = sorted(n for n in solo["node"].dropna().unique())
            profiles = sorted(solo["profile"].dropna().unique())
            med = solo.groupby(["profile", "node"])["makespan_s"].median()
            out["nodes"] = nodes
            out["matrix"] = {
                p: {n: (round(float(med[(p, n)])) if (p, n) in med else None) for n in nodes}
                for p in profiles
            }
        return out
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}


DIGEST_METRICS = [
    ("makespan_s", "время выполнения, с", "{:.1f}"),
    ("slowdown", "замедление", "{:.2f}×"),
    ("placement_regret", "ошибка размещения", "{:.3f}"),
]


def analysis_digest(report_dir: Path) -> dict:
    """Компактная выжимка из comparisons.csv: на каждый сценарий и метрику —
    среднее SensitivityScore и средние соперников с Holm-p и Cliff's δ. Строки
    без данных (конфигурации B/C/D) молча отбрасываются — это не «ждём», а
    «неприменимо на этом стенде». Читаем структурированный csv, а не сырой
    summary.md."""
    csv = report_dir / "comparisons.csv"
    if not csv.exists():
        return {"exists": False}
    try:
        import pandas as pd

        df = pd.read_csv(csv)
        df = df[df["mean_b"].notna() & (df["mw_n_b"].fillna(0) > 0)]
        scenarios: dict = {}
        for sc, g in df.groupby("scenario"):
            metrics = []
            # Строка вердикта — на КАЖДЫЙ профиль жертвы отдельно. Смешивать
            # профили нельзя: в сериях с двумя+ профилями (близнецы, смешанная)
            # старый код брал SS из первой строки метрики (чувствительный
            # профиль), а соперника — из последней (нечувствительный двойник,
            # вдвое быстрее по своей природе) и показывал «SS значимо хуже».
            n_profiles = g["profile"].nunique()
            for col, label, fmt in DIGEST_METRICS:
                for prof, gm in g[g["metric"] == col].groupby("profile"):
                    row_label = f"{label} · {prof}" if n_profiles > 1 else label
                    ss_mean = float(gm.iloc[0]["mean_a"])
                    opponents = {}
                    for r in gm.to_dict("records"):
                        holm = r.get("mw_p_holm")
                        sig = bool(pd.notna(holm) and holm < 0.05)
                        opponents[r["config_b"]] = {
                            "mean": float(r["mean_b"]),
                            "p_holm": None if pd.isna(holm) else float(holm),
                            "delta": None if pd.isna(r.get("cliffs_delta")) else float(r["cliffs_delta"]),
                            "better": float(r["mean_a"]) < float(r["mean_b"]),
                            "sig": sig,
                        }
                    metrics.append(
                        {"col": col, "label": row_label, "fmt": fmt,
                         "ss": ss_mean, "opponents": opponents}
                    )
            if metrics:
                scenarios[sc] = metrics
        return {"exists": True, "scenarios": scenarios,
                "mtime": time.strftime("%H:%M:%S", time.localtime(csv.stat().st_mtime))}
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}
