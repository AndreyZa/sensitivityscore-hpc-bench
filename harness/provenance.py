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
import os
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
    "profile_overrides",
    "bios_profile",
)


def profile_overrides() -> str:
    """Активные HARNESS_OVERRIDE_* -> "HIGH_S_PRIMARIES=40000;HIGH_S_THREADS=2".

    Это та самая дыра, ради которой стоит отдельная колонка: доза нагрузки
    (N_PRIMARIES, THREADS, число burst'ов) задаётся переменной окружения в
    run-скрипте и в конфиг не попадает. Две серии io-sensitivity с дозой,
    отличающейся вдвое, давали parquet, неразличимые по содержимому — а
    именно доза определяет измеряемое cˢ. Хеша конфига здесь мало: нужны
    сами значения, иначе по данным не восстановить, что было подано.
    """
    prefix = "HARNESS_OVERRIDE_"
    items = sorted((k[len(prefix):], v) for k, v in os.environ.items() if k.startswith(prefix))
    return ";".join(f"{k}={v}" for k, v in items)


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)
        return r.stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("провенанс: %s не отработал (%s)", " ".join(cmd[:2]), exc)
        return ""


def harness_commit(repo: Path | None = None) -> str:
    """HEAD харнесса; суффикс -dirty, если поверх коммита были правки.

    Грязное дерево помечается намеренно: «серия снята коммитом abc123» неверно,
    если поверх него были незакоммиченные правки, а именно так и идёт работа
    между сериями.

    --untracked-files=no ОБЯЗАТЕЛЕН, и это не мелочь. Прогон оставляет рядом
    свои же рабочие файлы (harness/.statuspage-<серия>.yaml и подобные), и с
    учётом неотслеживаемых КАЖДАЯ серия помечалась бы «-dirty» — а тогда флаг
    перестаёт различать «поверх коммита были правки кода» и «рядом валяется
    временный файл». Пойман 19.08.2026 на прод-стенде перед ночным прогоном.
    Неотслеживаемый файл не меняет того кода, который исполнялся: всё, что
    исполняется, отслеживается, а новый модуль потребовал бы правки
    отслеживаемого импорта — и она бы дерево пометила.
    """
    repo = repo or Path(__file__).resolve().parent.parent
    head = _run(["git", "-C", str(repo), "rev-parse", "--short=12", "HEAD"])
    if not head:
        return ""
    dirty = _run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"])
    return f"{head}-dirty" if dirty else head


def config_sha256(cfg: dict) -> str:
    """sha256 РАЗРЕШЁННОГО конфига серии — ловит правку между прогонами.

    Считается по итоговому словарю, а не по файлу серии: с появлением
    `extends` (config_loader.py) файл серии больше не определяет прогон
    целиком — правка родителя изменила бы поведение, не тронув хеш. Хешируем
    то, что реально управляло прогоном.

    Канонизация (sort_keys) нужна, чтобы перестановка ключей не создавала
    «другой» конфиг; default=str — на случай не-JSON типов из YAML (даты).
    """
    try:
        canon = json.dumps(cfg, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        log.warning("провенанс: конфиг не канонизируется (%s)", exc)
        return ""
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


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


def bios_profile(monitoring_namespace: str = "sensitivityscore-monitoring") -> str:
    """Настройки BIOS измерительных узлов -> "wrk-b6:SysProfile=...;ProcPwrPerf=...|wrk-b7:...".

    Зачем в провенансе строки результата. Это не справка о железе, а ПАРАМЕТРЫ
    МОДЕЛИ. `SysProfile`/`ProcPwrPerf` задают, кто и по какому критерию
    управляет частотой (на проде BIOS оптимизирует производительность на ватт,
    а не держит максимум). `EnergyEfficientTurbo` разрешает платформе САМОЙ
    снижать турбо на памяти-зависимых задачах — а memory-bound это ровно то,
    что создают агрессоры, и тогда замедление от интерференции и от снижения
    частоты в данных неразличимы. Префетчеры формируют промахи LLC, то есть
    главную ось. `SubNumaCluster` задаёт знаменатель NUMA-оси. Их тихая смена
    между кампаниями рассорит серии, и по данным это не восстановить — ровно
    так уже терялись калибровки 18.07.2026.

    Источник — Prometheus, а не iDRAC напрямую: доступ к BMC и пароль есть у
    поллера (он их и снимает раз в час), и заводить второй путь с теми же
    учётными данными ради провенанса незачем.

    Пусто = «не собрано»; как и у остальных полей, это честно читается как
    «доверять нельзя», и ошибка сбора не имеет права ронять серию.
    """
    query = "idrac_bios_attribute_info"
    raw = _run([
        "kubectl", "-n", monitoring_namespace, "exec", "deploy/prometheus",
        "-c", "prometheus", "--", "wget", "-qO-",
        f"http://localhost:9090/api/v1/query?query={query}",
    ], timeout=30)
    if not raw:
        return ""
    try:
        rows = json.loads(raw)["data"]["result"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""
    by_node: dict[str, dict[str, str]] = {}
    for row in rows:
        labels = row.get("metric", {})
        node = labels.get("node")
        name = labels.get("attribute")
        if not node or not name:
            continue
        by_node.setdefault(node, {})[name] = labels.get("value", "")
    if not by_node:
        return ""
    # Канонизация: порядок узлов и атрибутов не должен создавать «разные»
    # платформы там, где настройки одинаковы.
    return "|".join(
        node + ":" + ";".join(f"{k}={by_node[node][k]}" for k in sorted(by_node[node]))
        for node in sorted(by_node)
    )


def collect(cfg: dict, system_namespace: str) -> dict[str, str]:
    """Постоянная в пределах серии часть провенанса — собрать один раз."""
    prov = {
        "harness_commit": harness_commit(),
        "config_sha256": config_sha256(cfg),
        "workload_image": "",  # заполняется на строку: digest из imageID пода
        "calibration": calibration(system_namespace),
        "score_weights": score_weights(system_namespace),
        "profile_overrides": profile_overrides(),
        "bios_profile": bios_profile(),
    }
    # bios_profile не в optional намеренно: пустое значение здесь означает, что
    # платформу измерения нечем описать, и об этом надо знать до серии, а не
    # при разборе. Но и ронять прогон из-за него нельзя — только предупреждаем.
    optional = ("workload_image", "profile_overrides")
    missing = [k for k, v in prov.items() if not v and k not in optional]
    if missing:
        log.warning("провенанс собран не полностью: пусто %s", missing)
    else:
        log.info("провенанс: commit=%s config=%s calib=%s",
                 prov["harness_commit"], prov["config_sha256"], prov["calibration"])
    return prov
