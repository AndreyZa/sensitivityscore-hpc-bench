"""ml_slowdown.py — ML-вопрос №1: обыгрывает ли GBDT калиброванную линейную
модель цены осей в предсказании замедления? Offline, по уже собранным сериям
STAGE из ClickHouse; стендов не трогает.

Постановка. Линейная модель калибровки (calibrate_axis_costs.py):

    y = slowdown − 1 ≈ γ·nb + Σ_a p_a·(α_a + β_a·s_a) + intercept(серия)

— это ML нулевой ёмкости: регрессия с РУЧНЫМИ взаимодействиями p·s. Вопрос:
даёт ли модель следующей ступени ёмкости (градиентный бустинг, который сам
учит взаимодействия и нелинейности) меньшую out-of-sample ошибку НА ТЕХ ЖЕ
данных и том же информационном наборе? Ответ калибрует тон дальнейших глав:
«нет» — довод за интерпретируемую линейку на этих N; «да» — мотивация
нелинейного скоринга на прод-данных.

Честность сравнения:
  * те же строки, что в калибровке: серии LLC + смешанная, y = slowdown−1,
    датасет собирается ФУНКЦИЯМИ calibrate_axis_costs (никакого своего
    парсинга — расхождение с опубликованной калибровкой исключено);
  * одинаковый информационный набор: p_llc/io/net, s_llc/io/net, nb, серия.
    GBDT НЕ получает ручных произведений p·s — выучить взаимодействие его
    работа; линейка получает их, как в публикации. Идентичность узла — ни
    тому ни другому (давления и так функция узла; сырое имя узла дало бы
    GBDT запомнить среднее замедление узла в обход осей);
  * обе модели переобучаются в каждом фолде (линейка — тем же NNLS с
    посерийным интерсептом), метрика — out-of-fold MAE/RMSE;
  * фолды — GroupKFold по (серия, rep): повтор коррелирован внутри себя
    (общий поток, одна сессия), случайный сплит по строкам протёк бы той же
    псевдорепликацией, что вычищена в B3;
  * ΔMAE с 95% ДИ — кластерный бутстреп ПО ГРУППАМ (серия, rep) поверх
    out-of-fold ошибок, парно (одни и те же группы у обеих моделей).

Запуск (поверх make ch-tunnel):
    analysis/.venv/bin/python analysis/ml_slowdown.py
Плюс сверка: результат печатается и для наивного бейзлайна (среднее трейна) —
модель, не бьющая его, не обсуждается.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd
from scipy.optimize import nnls
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.model_selection import GroupKFold

import calibrate_axis_costs as cal

AXES = cal.AXES

FEATURES = [f"p_{a}" for a in AXES] + [f"s_{a}" for a in AXES] + ["nb"]


def load_dataset(host: str, port: int, stand: str,
                 mixed_label: str, llc_label: str, baselines_label: str
                 ) -> pd.DataFrame:
    from clickhouse_source import load_from_clickhouse
    ld = lambda tbl, lbl: load_from_clickhouse(
        tbl, host=host, port=port, stand=stand, run_labels=[lbl])
    base = cal.baseline_medians(ld("baselines", baselines_label))
    mixed = ld("results", mixed_label)
    llc = ld("results", llc_label)
    data = pd.concat([
        cal.build_rows(mixed, cal.node_pressures_mixed(mixed), base, "mixed3"),
        cal.build_rows(llc, cal.node_pressures_llc(llc), base, "llc"),
    ], ignore_index=True)
    # s_a восстанавливаются из декларации профиля (та же таблица SENSITIVITY,
    # что дала ps_a в build_rows) — GBDT получает сомножители, не произведение.
    for a in AXES:
        data[f"s_{a}"] = data["profile"].map(lambda pr: cal.SENSITIVITY[pr][a])
    data["group"] = data["series"] + "/" + data["rep"].astype(str)
    return data


def fit_linear(train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    """NNLS-линейка калибровки, переобученная на train: [p, p·s, nb, серия].
    Интерсепт серии у теста берётся из train (серии в обоих фолдах те же —
    GroupKFold делит повторы, не серии)."""
    cols = [f"p_{a}" for a in AXES] + [f"ps_{a}" for a in AXES] + ["nb"]
    series = sorted(train["series"].unique())
    X_tr = np.column_stack(
        [train[cols].to_numpy(float)]
        + [(train["series"] == s).to_numpy(float) for s in series])
    coef, _ = nnls(X_tr, train["y"].to_numpy(float))
    X_te = np.column_stack(
        [test[cols].to_numpy(float)]
        + [(test["series"] == s).to_numpy(float) for s in series])
    return X_te @ coef


def fit_gbdt(train: pd.DataFrame, test: pd.DataFrame, seed: int) -> np.ndarray:
    """GBDT на сомножителях (p, s, nb) + серия как категория. Малые данные —
    консервативные параметры и ранняя остановка по внутренней валидации."""
    def enc(df: pd.DataFrame, series: list[str]) -> np.ndarray:
        return np.column_stack(
            [df[FEATURES].to_numpy(float)]
            + [(df["series"] == s).to_numpy(float) for s in series])
    series = sorted(train["series"].unique())
    m = HistGradientBoostingRegressor(
        loss="absolute_error", max_iter=500, learning_rate=0.05,
        max_leaf_nodes=15, min_samples_leaf=20, l2_regularization=1.0,
        early_stopping=True, validation_fraction=0.15, random_state=seed)
    m.fit(enc(train, series), train["y"].to_numpy(float))
    return m.predict(enc(test, series))


def oof_predictions(data: pd.DataFrame, n_splits: int, seed: int) -> pd.DataFrame:
    out = data.copy()
    out["pred_lin"] = np.nan
    out["pred_gbdt"] = np.nan
    out["pred_mean"] = np.nan
    gkf = GroupKFold(n_splits=n_splits)
    for tr_idx, te_idx in gkf.split(data, groups=data["group"]):
        tr, te = data.iloc[tr_idx], data.iloc[te_idx]
        out.iloc[te_idx, out.columns.get_loc("pred_lin")] = fit_linear(tr, te)
        out.iloc[te_idx, out.columns.get_loc("pred_gbdt")] = fit_gbdt(tr, te, seed)
        out.iloc[te_idx, out.columns.get_loc("pred_mean")] = tr["y"].mean()
    return out


def cluster_bootstrap_delta(oof: pd.DataFrame, a: str, b: str,
                            n_boot: int, seed: int) -> tuple[float, float, float]:
    """Δ = MAE(a) − MAE(b) и 95% ДИ; ресемпл групп (серия, rep) целиком,
    парно — обе модели оцениваются на одном и том же ресемпле."""
    rng = np.random.default_rng(seed)
    groups = {k: idx for k, idx in oof.groupby("group").groups.items()}
    keys = list(groups)
    err_a = (oof[a] - oof["y"]).abs()
    err_b = (oof[b] - oof["y"]).abs()
    delta = float(err_a.mean() - err_b.mean())
    draws = []
    for _ in range(n_boot):
        pick = rng.integers(0, len(keys), len(keys))
        idx = np.concatenate([groups[keys[i]].to_numpy() for i in pick])
        draws.append(float(err_a.loc[idx].mean() - err_b.loc[idx].mean()))
    return delta, float(np.percentile(draws, 2.5)), float(np.percentile(draws, 97.5))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ch-host", default="localhost")
    ap.add_argument("--ch-port", type=int, default=8123)
    ap.add_argument("--stand", default="stage")
    ap.add_argument("--mixed-label", default="stage-mixed")
    ap.add_argument("--llc-label", default="stage-llc")
    ap.add_argument("--baselines-label", default="stage-mixed")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--bootstrap", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20260807,
                    help="дата постановки вопроса — фикс для воспроизводимости")
    ap.add_argument("--out-json", default=None)
    args = ap.parse_args()

    data = load_dataset(args.ch_host, args.ch_port, args.stand,
                        args.mixed_label, args.llc_label, args.baselines_label)
    n_groups = data["group"].nunique()
    print(f"строк: {len(data)}, групп (серия, rep): {n_groups}, "
          f"фолдов: {args.splits}")

    oof = oof_predictions(data, args.splits, args.seed)

    def mae(c): return float((oof[c] - oof["y"]).abs().mean())
    def rmse(c): return float(np.sqrt(((oof[c] - oof["y"]) ** 2).mean()))

    print("\n=== Out-of-fold ошибка предсказания y = slowdown − 1 ===")
    print(f"{'модель':<28}{'MAE':>9}{'RMSE':>9}")
    for name, col in [("наивная (среднее трейна)", "pred_mean"),
                      ("линейная (NNLS, p·s)", "pred_lin"),
                      ("GBDT (p, s — сам ищет)", "pred_gbdt")]:
        print(f"{name:<28}{mae(col):>9.4f}{rmse(col):>9.4f}")

    d, lo, hi = cluster_bootstrap_delta(
        oof, "pred_gbdt", "pred_lin", args.bootstrap, args.seed)
    verdict = ("GBDT ЛУЧШЕ (ДИ < 0)" if hi < 0 else
               "ЛИНЕЙНАЯ НЕ ХУЖЕ (ДИ накрывает 0)" if lo <= 0 <= hi else
               "GBDT ХУЖЕ (ДИ > 0)")
    print(f"\nΔMAE (GBDT − линейная) = {d:+.4f}, 95% ДИ [{lo:+.4f}; {hi:+.4f}] "
          f"— {verdict}")
    print(f"(кластерный бутстреп по {n_groups} группам (серия, rep), "
          f"{args.bootstrap} итераций, seed {args.seed})")

    if args.out_json:
        payload = {
            "n_rows": len(data), "n_groups": n_groups, "splits": args.splits,
            "mae": {c: mae(f"pred_{c}") for c in ("mean", "lin", "gbdt")},
            "rmse": {c: rmse(f"pred_{c}") for c in ("mean", "lin", "gbdt")},
            "delta_mae_gbdt_minus_lin": {"point": d, "ci95": [lo, hi]},
            "seed": args.seed,
        }
        with open(args.out_json, "w") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"json: {args.out_json}")


if __name__ == "__main__":
    main()
