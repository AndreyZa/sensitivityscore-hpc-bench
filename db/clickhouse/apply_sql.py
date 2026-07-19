#!/usr/bin/env python3
"""Применить .sql-файлы (схема, миграции) к ClickHouse по HTTP.

Зачем отдельный скрипт вместо clickhouse-client: приёмник доступен по HTTP
(8123) — так ходит загрузчик (load_parquet.py, clickhouse_connect), и так же
проброшен SSH-туннель к домашнему агрегатору (make ch-tunnel). Прежние цели
ch-schema/ch-migrate вызывали clickhouse-client на порту 9000, то есть по
НАТИВНОМУ протоколу: через туннель они не работали в принципе, а сам бинарь
ставится отдельно от venv и на машине его может не быть. Теперь у схемы,
миграций и загрузки один транспорт и одна зависимость.

    python apply_sql.py --host localhost --port 8123 migrations/*.sql
    python apply_sql.py --dry-run migrations/*.sql   # только разбор, без сети

Идемпотентность обеспечивают сами файлы (CREATE/ADD COLUMN IF NOT EXISTS), а
не этот скрипт: журнала применённых миграций тут нет намеренно — их пока
единицы, и лишний слой состояния дороже пользы.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def split_statements(sql: str) -> list[str]:
    """SQL-текст -> список отдельных выражений.

    Комментарии срезаем ДО разбиения: в наших файлах шапки многострочные и
    содержат в том числе точки с запятой в примерах команд, а они разорвали бы
    выражение пополам. Строковых литералов с ';' в схеме нет — если появятся,
    этот разбор придётся усложнить, поэтому проверяем и говорим вслух.
    """
    no_comments = re.sub(r"--[^\n]*", "", sql)
    quoted = re.findall(r"'[^']*'", no_comments)
    if any(";" in q for q in quoted):
        raise ValueError(
            "в SQL есть ';' внутри строкового литерала — примитивное "
            "разбиение по ';' его разорвёт, нужен нормальный парсер"
        )
    return [s.strip() for s in no_comments.split(";") if s.strip()]


def main() -> int:
    p = argparse.ArgumentParser(description="применить .sql к ClickHouse по HTTP")
    p.add_argument("files", nargs="+", help="файлы .sql в порядке применения")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8123)
    p.add_argument("--user", default="default")
    p.add_argument("--password", default="")
    p.add_argument("--database", default="", help="БД по умолчанию (можно пусто)")
    p.add_argument("--dry-run", action="store_true",
                   help="разобрать файлы и напечатать выражения, никуда не ходя")
    args = p.parse_args()

    batches: list[tuple[Path, list[str]]] = []
    for f in args.files:
        path = Path(f)
        if not path.is_file():
            print(f"ОШИБКА: нет файла {path}", file=sys.stderr)
            return 1
        try:
            batches.append((path, split_statements(path.read_text())))
        except ValueError as e:
            print(f"ОШИБКА в {path}: {e}", file=sys.stderr)
            return 1

    total = sum(len(s) for _, s in batches)
    print(f"файлов: {len(batches)}, выражений: {total}")

    if args.dry_run:
        for path, stmts in batches:
            print(f"\n-> {path}")
            for i, s in enumerate(stmts, 1):
                head = " ".join(s.split())[:100]
                print(f"   [{i}] {head}{'...' if len(head) == 100 else ''}")
        print("\n(dry-run — ничего не применено)")
        return 0

    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=args.host, port=args.port, username=args.user,
        password=args.password, database=args.database or None,
    )
    version = client.query("SELECT version()").result_rows[0][0]
    print(f"приёмник {args.host}:{args.port} — ClickHouse {version}")

    for path, stmts in batches:
        print(f"-> {path}")
        for i, s in enumerate(stmts, 1):
            head = " ".join(s.split())[:70]
            try:
                client.command(s)
            except Exception as e:  # noqa: BLE001 — печатаем, какое именно упало
                print(f"   [{i}] ПАДЕНИЕ: {head}...\n       {e}", file=sys.stderr)
                return 1
            print(f"   [{i}] ok: {head}{'...' if len(head) == 70 else ''}")
    print("применено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
