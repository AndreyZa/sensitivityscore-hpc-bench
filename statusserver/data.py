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
        exp_sc = expected_by_scenario(cfg)
        scenarios: dict = {}
        if "scenario" in df and "config" in df:
            dfp = df[df["scenario"].astype(str).str.startswith("pressure:")]
            for sc, g in dfp.groupby("scenario"):
                storm_nodes = storm.get(sc, set())
                tox = toxic.get(sc)
                arms: dict = {}
                for arm, ga in g.groupby("config"):
                    bad = (
                        ga["approximation"].astype(str).str.startswith("error:")
                        if "approximation" in ga
                        else pd.Series(False, index=ga.index)
                    )
                    ok = ga[ga["makespan_s"].notna() & ~bad]
                    if tox:
                        # Смешанный сценарий: «в шторм» = на узел, токсичный
                        # именно для профиля этой задачи.
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
                    # Смешанный сценарий: счётчик «в шторм» — НОМИНАЛЬНОЕ
                    # совпадение с декларированной осью; при калиброванных
                    # ценах осей узел дешёвой оси — намеренный выбор, судить
                    # качество надо по ошибке размещения (см. render).
                    "nominal": bool(tox),
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
            for col, label, fmt in DIGEST_METRICS:
                gm = g[g["metric"] == col]
                if gm.empty:
                    continue
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
                    {"col": col, "label": label, "fmt": fmt,
                     "ss": ss_mean, "opponents": opponents}
                )
            if metrics:
                scenarios[sc] = metrics
        return {"exists": True, "scenarios": scenarios,
                "mtime": time.strftime("%H:%M:%S", time.localtime(csv.stat().st_mtime))}
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": str(e)}
