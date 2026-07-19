"""config_loader.py — конфиг серии как слой поверх родительского.

Девять конфигов серий несли по 107-134 строки, из которых 11 верхнеуровневых
ключей из 18 были побайтово одинаковы во всех девяти. Дублирование здесь не
косметика: правка «общего» параметра (repetitions, cooldown, ёмкость узла)
требовала девяти согласованных правок, и любая пропущенная делала серии
несравнимыми — молча, потому что конфиг остаётся валидным.

Ключ `extends: <файл>` подмешивает родителя; путь разрешается относительно
самого конфига. Цепочка любой длины: base -> стенд -> серия.

Семантика слияния:
  * словари сливаются рекурсивно;
  * СПИСКИ ЗАМЕЩАЮТСЯ целиком, а не дополняются. Серия, переопределяющая
    pressure_scenarios или profiles, задаёт свой набор — дополнение молча
    подмешало бы чужие сценарии в прогон;
  * `extends` в результат не попадает.
"""

from __future__ import annotations

from pathlib import Path

import yaml

EXTENDS_KEY = "extends"


def deep_merge(base: dict, override: dict) -> dict:
    """Родитель + потомок; потомок сильнее. Списки замещаются (см. модуль)."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path, _seen: tuple[Path, ...] = ()) -> dict:
    """Прочитать конфиг, развернув цепочку `extends`."""
    path = Path(path).resolve()
    if path in _seen:
        chain = " -> ".join(p.name for p in (*_seen, path))
        raise ValueError(f"цикл в extends: {chain}")

    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    parent_ref = cfg.pop(EXTENDS_KEY, None)
    if parent_ref is None:
        return cfg

    parent_path = (path.parent / parent_ref).resolve()
    if not parent_path.exists():
        raise FileNotFoundError(f"{path.name}: extends указывает на несуществующий {parent_ref}")
    return deep_merge(load_config(parent_path, (*_seen, path)), cfg)


def config_chain(path: str | Path) -> list[str]:
    """Цепочка файлов от потомка к корню — для диагностики и логов."""
    chain: list[str] = []
    current = Path(path).resolve()
    while True:
        chain.append(current.name)
        with open(current) as f:
            cfg = yaml.safe_load(f) or {}
        parent = cfg.get(EXTENDS_KEY)
        if not parent:
            return chain
        current = (current.parent / parent).resolve()
        if current.name in chain:
            return chain
