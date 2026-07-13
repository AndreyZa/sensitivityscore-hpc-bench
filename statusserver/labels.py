"""Подписи планировщиков/сценариев и текстовые помощники (в т.ч. русские
словоформы для формул объёма — «3 узла», а не «3 узлов»)."""

from __future__ import annotations

import html

# Порядок и подписи сравниваемых планировщиков. Внутренние имена конфигураций
# ("A-default") на страницу не выводятся — читателю важен планировщик, а не
# код инфраструктурной конфигурации.
ARM_ORDER = ["A-default", "A-sensitivityscore", "A-trimaran"]
ARM_LABEL = {
    "A-default": "default",
    "A-sensitivityscore": "SensitivityScore",
    "A-trimaran": "trimaran",
}
# Исследуемый планировщик (его строка в таблицах подсвечивается).
HERO_ARM = "A-sensitivityscore"

SCENARIO_LABEL = {
    "pressure:io": "Диск (IO): фоновая дисковая нагрузка",
    "pressure:net": "Сеть (Net): фоновая сетевая нагрузка",
    "pressure:llc": "Кэш (LLC): фоновая нагрузка на кэш и память",
}

# Профиль задачи -> сценарий: запасной словарь для строк лога, снятых с
# другим конфигом. Основной источник — сам config.yaml (см. ниже).
PROFILE_SCENARIO = {
    "high-s-io": "pressure:io",
    "high-s-net": "pressure:net",
    "high-s": "pressure:llc",
}


def profile_scenario_map(cfg: dict) -> dict[str, str]:
    """Профиль-жертва -> колонка сценария (pressure:<name>) из config.yaml;
    статический PROFILE_SCENARIO остаётся запасным."""
    out = dict(PROFILE_SCENARIO)
    for sc in cfg.get("pressure_scenarios", []):
        out[sc.get("victim_profile", "high-s")] = f"pressure:{sc['name']}"
    return out


def esc(s) -> str:
    return html.escape(str(s))


def arm_label(cfg_name: str) -> str:
    return ARM_LABEL.get(cfg_name, str(cfg_name).replace("A-", ""))


def scenario_label(s: str) -> str:
    return SCENARIO_LABEL.get(s, str(s).replace("pressure:", ""))


def ru(n: int, one: str, few: str, many: str) -> str:
    """«N слово» с согласованием: ru(3,'узел','узла','узлов') -> «3 узла»."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        form = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        form = few
    else:
        form = many
    return f"{n} {form}"
