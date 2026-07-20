"""node_pressure.py — снапшот давления нод + расчёт placement regret.

Regret размещения — прямая метрика качества решения планировщика, в отличие
от makespan (конечный исход, на который влияет много шума):

    regret = interference(выбранная нода) - min по нодам interference(нода),

где interference — та же нормированная скор-функция, что в плагине
SensitivityScore (dot(S_job, Pressure_node) с весами по всем четырём осям,
включая Net через net_pressure — см. pkg/sensitivityscore/sensitivityscore.go
в форке scheduler-plugins; на стенде без Net-калибровки ось выключается весом
net=0). Идеальный interference-aware планировщик даёт regret ~= 0;
слепой к интерференции — большой regret ровно в pressure-сценариях. Метрика
почти детерминированная, эффект виден даже там, где makespan шумит.

Считается ХАРНЕССОМ, одинаково для всех плеч: в плече A-default плагин вообще
не запущен, так что логи планировщика не подходят — а Redis-снапшот
node:metrics:* доступен всегда, пока жив metrics-agent. Снапшот берётся в
момент сабмита; фактическое решение планировщик принимает на секунды позже по
своему кэшу (refresh до 10с) — для минутных окон давления агрессоров это
несущественно, но оговаривается в методике.
"""

from __future__ import annotations

import logging
import math
import time

import redis

from profiles import Sensitivity
from submit.redis_metrics import _connect

# Зеркало extractSensitivityVector плагина: high|medium|low -> 1.0|0.5|0.0
# (всё нераспознанное = 0.0, как в плагине).
SENSITIVITY_VALUE = {"high": 1.0, "medium": 0.5}

# Зеркало defaultWeights() плагина; переопределяется score_weights из
# config.yaml. Должно совпадать с weights.json в ConfigMap sensitivity-config
# (k8s/scheduler-config/sensitivity-configmap.yaml), иначе regret считается
# не по той скор-функции, которой реально пользуется плагин.
DEFAULT_WEIGHTS = {"llc": 1.0, "numa": 1.0, "net": 1.0, "io": 1.0}


def split_weights(weights: dict) -> tuple[dict[str, float], dict[str, float]]:
    """-> (base, sensitivity) по осям — зеркало parseWeights плагина.

    Два формата weights.json/score_weights (см. scoreWeights в
    pkg/sensitivityscore/sensitivityscore.go форка): новый
    {"base": {...}, "sensitivity": {...}} — вклад оси (base + sens*s)*p,
    базовую цену платит любая задача узла (калибровка STAGE: β=0, дисковый
    шторм замедляет все задачи); легаси-плоский {"llc": 1.0, ...} — это
    sensitivity с base=0, прежнее поведение."""
    if "base" in weights or "sensitivity" in weights:
        base, sens = weights.get("base", {}), weights.get("sensitivity", {})
    else:
        base, sens = {}, weights
    return ({a: float(base.get(a, 0.0)) for a in AXES},
            {a: float(sens.get(a, 0.0)) for a in AXES})

# Оси скор-функции — порядок и состав зеркалят nodePressure плагина.
AXES = ("llc", "numa", "net", "io")


log = logging.getLogger(__name__)

# Снимок берётся ОДИН раз на сабмит и второго шанса не будет: пересчитать
# regret постфактум нечем — ландшафт давления к тому времени уже другой.
SNAPSHOT_ATTEMPTS = 3
SNAPSHOT_RETRY_DELAY_SECONDS = 2


def _clamp01(v: float) -> float:
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def snapshot_node_pressure(redis_addr: str, attempts: int = SNAPSHOT_ATTEMPTS
                           ) -> dict[str, dict[str, float]]:
    """Читает все node:metrics:<node> хэши -> {node: {llc, numa, io}}, каждая
    ось в [0,1] (тот же clamp, что parsePressureField в redis_source.go
    плагина). Никогда не кидает: сломанный метрик-пайплайн не должен ронять
    сабмит (regret тогда честно NaN), та же политика, что у fetch_job_metrics.

    Но «никогда не кидает» не значит «сдаётся с первого раза»: Redis виден
    через port-forward, а тот идёт сквозь API-сервер, и мгновенная заминка
    последнего (`TLS handshake timeout` на облачном стенде) стоила бы строке
    метрики решения. Пробуем несколько раз; если не вышло — пишем, ПОЧЕМУ,
    иначе NaN в regret не с чем связать (так и потеряли 120 строк 20.07)."""
    for attempt in range(1, max(1, attempts) + 1):
        snapshot = _read_snapshot(redis_addr)
        if snapshot is not None:
            return snapshot
        if attempt < attempts:
            time.sleep(SNAPSHOT_RETRY_DELAY_SECONDS)
    log.warning(
        "снимок давления узлов не получен за %d попыток (%s) — "
        "placement_regret этой строки будет NaN", attempts, redis_addr)
    return {}


def _read_snapshot(redis_addr: str) -> dict[str, dict[str, float]] | None:
    """Одна попытка. None — Redis не ответил (в отличие от {} «ключей нет»)."""
    snapshot: dict[str, dict[str, float]] = {}
    try:
        r = _connect(redis_addr)
        for key in r.scan_iter(match="node:metrics:*"):
            node = key.removeprefix("node:metrics:")
            if not node:
                continue
            fields = r.hgetall(key)

            def _field(name: str) -> float:
                # Отсутствующее поле = 0.0, как и немпарсящееся: NaN здесь
                # отравил бы interference ноды и min() по всем нодам — при
                # version skew агента (нода со старым образом не пишет новое
                # поле, случалось на STAGE) regret молча стал бы NaN у всей
                # серии. Ноль честен ровно в этом сценарии: у отсутствующей
                # оси вес обычно 0 (стенд её не меряет), вклад и так нулевой.
                raw = fields.get(name)
                if raw is None:
                    return 0.0
                try:
                    return _clamp01(float(raw))
                except ValueError:
                    return 0.0

            snapshot[node] = {
                "llc": _field("llc_miss_rate"),
                "numa": _field("numa_remote_ratio"),
                # net_pressure — нормированная Net-ось (net_bw / калибровка
                # NET_REFERENCE_MBPS стенда); без калибровки агент пишет 0.
                "net": _field("net_pressure"),
                "io": _field("io_pressure"),
            }
    except redis.exceptions.RedisError:
        return None
    return snapshot


def interference(
    sensitivity: Sensitivity, pressure: dict[str, float], weights: dict
) -> float:
    """Нормированная интерференция S_job x Pressure_node в [0,1] — та же
    формула, что interferenceScore() плагина (у него pressure в шкале 0-100 и
    знаменатель * 100; здесь обе части в [0,1], результат идентичен): вклад
    оси = (base + sensitivity*s) * p, знаменатель Σ(base + sensitivity)."""
    base, sens = split_weights(weights)
    denom = sum(base.values()) + sum(sens.values())
    if denom <= 0:
        return 0.0
    s = {
        axis: SENSITIVITY_VALUE.get(getattr(sensitivity, axis), 0.0)
        for axis in AXES
    }
    dot = sum(
        (base[axis] + sens[axis] * s[axis]) * pressure.get(axis, 0.0)
        for axis in AXES
    )
    return dot / denom


def placement_regret(
    sensitivity: Sensitivity,
    snapshot: dict[str, dict[str, float]],
    chosen_node: str | None,
    weights: dict[str, float],
) -> tuple[float, float]:
    """-> (interference_chosen, regret), оба в [0,1] или NaN.

    NaN, когда выбранная нода не в снапшоте (агент на ней не писал, TTL истёк,
    Slurm-имя ноды не совпало с K8s-именем) или снапшот пуст — строка остаётся
    валидной по makespan, просто без regret. min берётся по нодам снапшота:
    это ноды с живым metrics-agent, т.е. ровно то множество, по которому и
    плагин видит давление (остальным он даёт нейтральный score)."""
    if not snapshot or not chosen_node or chosen_node not in snapshot:
        return float("nan"), float("nan")
    chosen = interference(sensitivity, snapshot[chosen_node], weights)
    best = min(interference(sensitivity, p, weights) for p in snapshot.values())
    regret = chosen - best
    # Защита от -0.0 из плавающей арифметики в колонке результата.
    return chosen, 0.0 if math.isclose(regret, 0.0, abs_tol=1e-12) else regret
