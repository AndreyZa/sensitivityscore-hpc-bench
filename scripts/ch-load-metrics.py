#!/usr/bin/env python3
"""Инкрементальная выгрузка рядов Prometheus прод-стенда в ClickHouse.

Зачем не «подливать бэкап TSDB». Архив из make monitoring-backup — это блоки
Prometheus в его внутреннем формате: чтобы достать оттуда точки, нужен
promtool tsdb dump, и каждый раз это разбор ВСЕЙ истории заново, без понятия
«что уже загружено». Архив остаётся тем, чем он и является — средством поднять
Prometheus обратно. А для ClickHouse правильный источник — HTTP API самого
Prometheus: он отдаёт точный диапазон, шаг и метки, и позволяет догружать
инкрементально по водяному знаку.

Почему closed-list метрик, а не «всё». В TSDB прода ~115 тысяч рядов, и
подавляющая часть — cAdvisor и kubelet: они нужны для дежурства, а не для
диссертации, и залив их означал бы сотни миллионов строк в сутки. Здесь только
то, что описывает СТЕНД И ИЗМЕРЕНИЕ.

Запускать С ЛАБЫ: там есть и kubectl к прод-кластеру, и лабный ClickHouse на
localhost:8123 (hostPort). Prometheus опрашивается через `kubectl exec`, а не
через проброс: лишний постоянный туннель ради выгрузки раз в сутки не нужен.

    make ch-load-metrics                 # с водяного знака до now
    make ch-load-metrics SINCE=48h       # принудительно за последние 48 часов
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# Закрытый список. Комментарий у каждой группы — почему она переживёт ретеншен
# Prometheus, то есть зачем вообще нужна через год.
METRICS = [
    # Оси чувствительности: то, ради чего стенд существует. Без них нельзя
    # задним числом ответить, что творилось на узле в минуту конкретной точки
    # плана.
    "ss_node_llc_miss_rate",
    "ss_node_llc_misses_per_sec",
    "ss_node_io_pressure",
    "ss_node_io_iops",
    "ss_node_net_bw_bytes_per_second",
    "ss_node_net_pressure",
    "ss_node_numa_remote_ratio",
    "ss_node_numa_dram_events_per_sec",
    # Годность сбора в тот же момент: ось, снятая при синтетическом PMU или
    # мультиплексировании, негодна — и узнать это надо при разборе, а не когда
    # ряды уже стёрты ретеншеном.
    "ss_agent_pmu_hardware_available",
    "ss_agent_pmu_multiplex_ratio",
    "ss_agent_llc_calibrated",
    "ss_agent_net_calibrated",
    "ss_agent_psi_available",
    "ss_agent_sampled_pods",
    "ss_agent_sample_errors_total",
    # Энерговетка: накопительные счётчики и мощность. energy_windows хранит
    # уже посчитанные окна, здесь — исходные ряды, по которым окно можно
    # пересчитать при смене методики.
    "ss_node_rapl_joules_total",
    "ss_agent_rapl_zones",
    "idrac_power_watts",
    "idrac_psu_input_watts",
    # Стоечные устройства. Без них в базе не посчитать ни сверку приростов
    # с независимым прибором, ни дрейф накопительного регистра (Э0.3): и то
    # и другое приходилось делать прямыми запросами к Prometheus, а окно
    # старше ретеншена не переспросить. Регистр нужен целиком, а не только
    # мощность: разность на границах окна — самостоятельный источник энергии.
    "pdu_device_power_centikw",
    "pdu_device_energy_decikwh",
    "pdu_bank_current_deciamp",
    "pdu_phase_current_deciamp",
    # Реакция платформы. Плане расчётов требует их РЯДОМ с Дж/задача: без
    # эффективной частоты и троттлинга разность плеч нельзя разложить на
    # эффект политики и реакцию платформы на созданное ею давление (§9
    # статьи). Частота на этом железе идёт не из cpufreq, а из aperf/mperf.
    "ss_node_cpu_freq_hertz",
    "ss_node_cpu_throttle_events_total",
    # Зажатие жертв квотой CFS. Зажатая задача досчитывает дольше, и харнесс
    # запишет удлинение как замедление от интерференции — в логах не видно,
    # в цифрах неотличимо. Отношение этих двух и есть доля зажатых периодов.
    "container_cpu_cfs_periods_total",
    "container_cpu_cfs_throttled_periods_total",
    # Отпечаток настроек BIOS. Сами настройки строкой лежат в провенансе каждой
    # строки результата (колонка bios_profile), а сюда идёт число: по нему в
    # SQL видно, шли ли две серии на ОДНОЙ платформе, без разбора текста.
    "idrac_bios_profile_hash",
    # Узел работал не на полную — второй и третий способы объяснить
    # замедление не интерференцией.
    "ss_node_cpu_throttle_events_total",
    "ss_node_cpu_freq_hertz",
    "ss_agent_cpu_throttle_available",
    # Что видели load-aware плечи в момент решения: без этого сравнение плеч
    # через год нечем перепроверить.
    "ss_loadwatcher_node_value",
    "ss_loadwatcher_up",
    "ss_loadwatcher_nodes",
    # Границы серий: по ним ряды выше режутся на прогоны.
    #
    # Heartbeat нужен НЕ для красоты. Маркер живёт в pushgateway, а тот помнит
    # последнее значение вечно: если серия оборвалась вместе с хостом и снять
    # маркер было некому, ss_series_running продолжит скрейпиться единицей
    # бесконечно — и в ClickHouse серия «шла бы» до скончания века. Признак
    # свежести позволяет отрезать это в SQL:
    #
    #   SELECT labels['series'] AS серия, min(ts), max(ts)
    #   FROM sensitivityscore.metrics_samples
    #   WHERE metric = 'ss_series_running' AND value = 1
    #     AND (stand, labels['series'], ts) IN (
    #           SELECT stand, labels['series'], ts FROM sensitivityscore.metrics_samples
    #           WHERE metric = 'ss_series_heartbeat_seconds'
    #             AND toUnixTimestamp(ts) - value < 900)
    #   GROUP BY серия;
    #
    # Правило SSSeriesMarkerStale ловит тот же случай в момент, а не задним
    # числом.
    "ss_series_running",
    "ss_series_heartbeat_seconds",
]

NS = "sensitivityscore-monitoring"
DEFAULT_STEP = 30           # = scrape_interval стенда; мельче — выдумывать точки
CHUNK_HOURS = 12            # Prometheus отдаёт не более 11000 точек на ряд за запрос


def prom_query_range(metric, start, end, step, kubectl="kubectl"):
    """query_range через `kubectl exec` — без постоянного проброса."""
    qs = urllib.parse.urlencode({
        "query": metric, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": str(step)})
    url = f"http://localhost:9090/api/v1/query_range?{qs}"
    out = subprocess.run(
        [kubectl, "-n", NS, "exec", "deploy/prometheus", "-c", "prometheus", "--",
         "wget", "-qO-", url],
        capture_output=True, text=True, timeout=180)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError(f"{metric}: query_range не удался: {out.stderr.strip()[:200]}")
    body = json.loads(out.stdout)
    if body.get("status") != "success":
        raise RuntimeError(f"{metric}: {body.get('error')}")
    return body["data"]["result"]


def ch(sql, ch_url, data=None):
    req = urllib.request.Request(
        ch_url + "/?" + urllib.parse.urlencode({"query": sql}),
        data=data if data is not None else b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Тело ответа важнее кода: ClickHouse объясняет причину именно в нём
        # («Cannot parse input: expected ...»), а по 400 не догадаться.
        body = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"ClickHouse ответил {exc.code}: {body}") from exc


def watermark(ch_url, database, stand):
    """Последняя загруженная точка. Пусто -> None (значит, грузим с --since)."""
    sql = (f"SELECT toUnixTimestamp(max(ts)) FROM {database}.metrics_samples "
           f"WHERE stand = '{stand}'")
    try:
        raw = ch(sql, ch_url).strip()
    except Exception:
        return None
    if not raw or raw == "0":
        return None
    return float(raw)


def rows_from(result, metric, stand):
    for series in result:
        labels = dict(series.get("metric", {}))
        labels.pop("__name__", None)
        node = labels.pop("node", "")
        # Метки цели одинаковы у всех точек ряда и в анализе не нужны; их
        # хранение раздуло бы Map и ключ сортировки на ровном месте.
        for noise in ("instance", "job", "pod", "namespace", "endpoint",
                      "container", "pod_template_hash", "instance_node",
                      "app", "app_kubernetes_io_part_of"):
            labels.pop(noise, None)
        series_key = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
        for ts, value in series.get("values", []):
            # NaN/Inf в JSON приезжают строками — ClickHouse их не примет, а
            # для рядов это «точки не было», а не ноль.
            if value in ("NaN", "+Inf", "-Inf"):
                continue
            # ВРЕМЯ СТРОКОЙ, а не числом. ClickHouse принимает в DateTime64
            # число только как ЦЕЛЫЕ секунды и падает на дробной части
            # («Cannot parse input: expected ','»), а Prometheus отдаёт
            # timestamp с плавающей точкой. Строка в ISO разбирается
            # однозначно и не зависит от date_time_input_format.
            whole = int(ts)
            millis = int(round((float(ts) - whole) * 1000))
            if millis == 1000:                     # округление вверх на границе
                whole, millis = whole + 1, 0
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(whole)) + f".{millis:03d}"
            yield {
                "stand": stand, "metric": metric, "node": node,
                "series_key": series_key, "labels": labels,
                "ts": stamp, "value": float(value),
            }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ch-url", default="http://localhost:8123")
    ap.add_argument("--database", default="sensitivityscore")
    ap.add_argument("--stand", default="prod")
    ap.add_argument("--since", default="",
                    help="принудительное окно назад от now (24h, 7d); иначе — с водяного знака")
    ap.add_argument("--step", type=int, default=DEFAULT_STEP)
    ap.add_argument("--kubectl", default="kubectl")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    end = time.time()
    if args.since:
        mult = {"m": 60, "h": 3600, "d": 86400}
        unit = args.since[-1]
        if unit not in mult:
            sys.exit("--since: ожидается вид 30m / 24h / 7d")
        start = end - float(args.since[:-1]) * mult[unit]
        print(f"окно задано вручную: последние {args.since}")
    else:
        wm = watermark(args.ch_url, args.database, args.stand)
        if wm is None:
            start = end - 24 * 3600
            print("водяного знака нет (таблица пуста) — беру последние сутки")
        else:
            # Шаг назад на один интервал: точка на самой границе могла быть
            # записана не полностью, ReplacingMergeTree схлопнет повтор.
            start = wm - args.step
            print(f"с водяного знака: {time.strftime('%F %T', time.gmtime(start))} UTC")

    if end - start < args.step:
        print("нечего догружать")
        return

    total = 0
    for metric in METRICS:
        got = 0
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + CHUNK_HOURS * 3600, end)
            try:
                result = prom_query_range(metric, chunk_start, chunk_end, args.step, args.kubectl)
            except Exception as exc:
                print(f"  {metric}: пропускаю кусок ({exc})")
                chunk_start = chunk_end
                continue
            payload = "\n".join(json.dumps(r, ensure_ascii=False)
                                for r in rows_from(result, metric, args.stand))
            if payload:
                if not args.dry_run:
                    ch(f"INSERT INTO {args.database}.metrics_samples FORMAT JSONEachRow",
                       args.ch_url, payload.encode("utf-8"))
                got += payload.count("\n") + 1
            chunk_start = chunk_end
        total += got
        if got:
            print(f"  {metric}: {got} строк")
    print(f"{'(сухой прогон) ' if args.dry_run else ''}всего строк: {total}")


if __name__ == "__main__":
    main()
