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
    "pressure:mixed3": "Смешанный: три шторма (кэш+диск+сеть) одновременно",
    "pressure:mixed": "Смешанный: три шторма (кэш+диск+сеть) одновременно",
    "pressure:placebo": "Плацебо: без фоновой нагрузки (отрицательный контроль)",
    "pressure:io-sensitivity": "Диск: различение по чувствительности (вывод на критическом пути)",
    "pressure:differentiation": "Диск: цена чувствительности, задачи-близнецы с равными ресурсами",
    "pressure:net-diff": "Сеть: цена чувствительности, близнецы + egress-шторм",
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
        col = f"pressure:{sc['name']}"
        if "victims" in sc:  # смешанный сценарий: несколько профилей жертв
            for v in sc["victims"]:
                out[v["profile"]] = col
        else:
            out[sc.get("victim_profile", "high-s")] = col
    return out


def scenario_victim_count(sc: dict) -> int:
    """Число жертв на плечо: сумма по victims (смешанный сценарий) или
    victim_count (легаси)."""
    if "victims" in sc:
        return sum(int(v.get("count", 1)) for v in sc["victims"])
    return sc.get("victim_count", 6)


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
