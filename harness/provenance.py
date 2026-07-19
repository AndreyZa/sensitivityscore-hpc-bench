"""provenance.py — чем именно снята строка результата.

Зачем. Без провенанса строки девяти STAGE-серий неразличимы: тег образа
`:dev` за июль перезаписывался многократно, `imagePullPolicy: Always` означает,
что пуш нового образа переключает часть Job'ов даже внутри одной серии, а
фактические параметры дозы (IO_TOTAL_BURSTS и т.п.) живут в env-оверрайдах
run-скриптов и никуда не сохраняются. Две серии с дозой, отличающейся вдвое,
дают parquet, неразличимые по содержимому.

Отдельно — калибровки. Их молчаливая потеря уже случалась (18.07.2026): часть
серий шла с выключенной Net-осью и llc_miss_rate в другой шкале, и по данным
это не восстановить, потому что калибровка не записана рядом с результатом.

Всё, что здесь собирается, постоянно в пределах серии — собирается один раз
и подмешивается в каждую строку. Ни одна ошибка сбора не должна ронять серию:
недоступный kubectl или отсутствующий git дают пустое значение, а пустое
значение честно читается как «доверять нельзя».
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Колонки провенанса. Пустая строка = «не собрано»; в ClickHouse у них
# DEFAULT '', поэтому старые серии читаются так же честно.
PROVENANCE_COLUMNS = (
    "harness_commit",
    "config_sha256",
    "workload_image",
    "calibration",
    "score_weights",
)


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("провенанс: %s не отработал (%s)", " ".join(cmd[:2]), exc)
        return ""


def harness_commit(repo: Path | None = None) -> str:
    """HEAD харнесса; суффикс -dirty, если дерево грязное.

    Грязное дерево помечается намеренно: «серия снята коммитом abc123» неверно,
    если поверх него были незакоммиченные правки, а именно так и идёт работа
    между сериями.
    """
    repo = repo or Path(__file__).resolve().parent.parent
    head = _run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"])
    if not head:
        return ""
    dirty = _run(["git", "-C", str(repo), "status", "--porcelain"])
    return f"{head}-dirty" if dirty else head


def config_sha256(config_path: str | Path) -> str:
    """sha256 файла конфига серии — ловит правку конфига между прогонами.

    Не заменяет запись самих параметров, но отвечает на вопрос «тот ли это
    конфиг, что и в прошлый раз» однозначно и дёшево.
    """
    try:
        return hashlib.sha256(Path(config_path).read_bytes()).hexdigest()[:16]
    except OSError as exc:
        log.warning("провенанс: не прочитан конфиг %s (%s)", config_path, exc)
        return ""


def calibration(namespace: str, configmap: str = "metrics-agent-calibration") -> str:
    """Калибровки стенда из ConfigMap агента -> "llc=15000000;net=1616".

    Пустое значение здесь означает не «ошибка сбора», а «оси не калиброваны» —
    и то и другое одинаково важно видеть в данных: без калибровки LLC агент
    пишет сырой ratio (другая шкала), а net_pressure тождественно ноль.
    """
    raw = _run([
        "kubectl", "-n", namespace, "get", "configmap", configmap,
        "-o", "jsonpath={.data}",
    ])
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    parts = []
    if "LLC_REFERENCE_MISSES_PER_SEC" in data:
        parts.append(f"llc={data['LLC_REFERENCE_MISSES_PER_SEC']}")
    if "NET_REFERENCE_MBPS" in data:
        parts.append(f"net={data['NET_REFERENCE_MBPS']}")
    return ";".join(parts)


def score_weights(namespace: str, configmap: str = "sensitivity-config") -> str:
    """Веса скор-функции, реально загруженные в кластер, канонизированным JSON.

    Берутся из ConfigMap, а не из конфига серии: preflight сверяет их между
    собой, но веса меняются ручным `kubectl patch`, и забытый (или applied уже
    после старта) патч даёт расхождение, неотличимое post-hoc. В данные едет
    то, по чему реально скорил планировщик.
    """
    raw = _run([
        "kubectl", "-n", namespace, "get", "configmap", configmap,
        "-o", "jsonpath={.data.weights\\.json}",
    ])
    if not raw:
        return ""
    try:
        # Канонизация: порядок ключей не должен создавать «разные» веса.
        return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except json.JSONDecodeError:
        return raw[:200]


def collect(config_path: str | Path, system_namespace: str) -> dict[str, str]:
    """Постоянная в пределах серии часть провенанса — собрать один раз."""
    prov = {
        "harness_commit": harness_commit(),
        "config_sha256": config_sha256(config_path),
        "workload_image": "",  # заполняется на строку: digest из imageID пода
        "calibration": calibration(system_namespace),
        "score_weights": score_weights(system_namespace),
    }
    missing = [k for k, v in prov.items() if not v and k != "workload_image"]
    if missing:
        log.warning("провенанс собран не полностью: пусто %s", missing)
    else:
        log.info("провенанс: commit=%s config=%s calib=%s",
                 prov["harness_commit"], prov["config_sha256"], prov["calibration"])
    return prov
