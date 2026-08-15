"""Preparation targets for the staff Telegram READY notification.

The names below are normalized aliases for the live Alpha POS catalog. Targets
come from the restaurant's approved kitchen timing sheet. An order containing
multiple tracked products uses the longest target because those products can be
prepared in parallel and the order cannot be ready before its slowest item.
"""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class PreparationTarget:
    maximum_seconds: int
    display: str


@dataclass(frozen=True)
class PreparationPerformance:
    key: str
    icon: str
    label: str


GREEN = PreparationPerformance('ON_TIME', '🟢', 'VAQTIDA')
YELLOW = PreparationPerformance('SLIGHTLY_LATE', '🟡', 'OZGINA KECHIKDI')
RED = PreparationPerformance('VERY_LATE', '🔴', 'JUDA KECHIKDI')


def normalize_product_name(value):
    """Return a punctuation/case-insensitive catalog name for stable matching."""
    value = unicodedata.normalize('NFKD', str(value or '')).casefold()
    value = value.replace('ʻ', "'").replace('’', "'").replace('`', "'")
    value = re.sub(r"[^a-z0-9']+", ' ', value)
    return ' '.join(value.split())


def preparation_target_for_name(product_name):
    """Return the approved target for one live-catalog product name, if any."""
    name = normalize_product_name(product_name)

    if name in {'hot dog mini', 'xodok mini'}:
        return PreparationTarget(3 * 60, '3 daqiqa')
    if name in {'hot dog dabl', 'dabl hot dog', 'double hot dog', 'dabl xodok'}:
        return PreparationTarget(4 * 60, '4 daqiqa')
    if name in {
        'hot dog karalevskiy',
        'hot dog korolevskiy',
        'qora lavash',
        'kora lavash',
    }:
        return PreparationTarget(4 * 60, '4 daqiqa')

    if name.startswith('non burger'):
        return PreparationTarget(6 * 60, '6 daqiqa')
    if name.startswith('longer'):
        return PreparationTarget(5 * 60, '5 daqiqa')
    if name.startswith('toster') or name.startswith('nostar'):
        return PreparationTarget(5 * 60, '5 daqiqa')

    if (
        name.startswith('chicken burger')
        or (name.startswith('chicken ') and name.endswith(' burger'))
        or name.startswith('burger chikin')
    ):
        return PreparationTarget(8 * 60, '8 daqiqa')
    if name.startswith('burger donarli') or name.startswith('donar burger'):
        return PreparationTarget(8 * 60, '8 daqiqa')
    if name in {
        'burger',
        'burger chiz',
        'dabl burger',
        'dabl burger chiz',
        'smart burger',
    }:
        return PreparationTarget(20 * 60, '20 daqiqa')

    if 'pitsa' in name or 'pizza' in name:
        return PreparationTarget(20 * 60, '15–20 daqiqa')
    if name == 'kartoshka fri' or name == 'fri':
        return PreparationTarget(3 * 60, '3 daqiqa')
    if name.startswith('smart strips'):
        return PreparationTarget(8 * 60, '7–8 daqiqa')
    if name.startswith('qanotcha') or name.startswith('qanot'):
        return PreparationTarget(9 * 60, '8–9 daqiqa')
    if name.startswith('strips'):
        return PreparationTarget(8 * 60, '7–8 daqiqa')
    if name.startswith('naggetsi') or name.startswith('nuggets'):
        return PreparationTarget(6 * 60, '5–6 daqiqa')
    if name == 'file' or name.startswith('file '):
        return PreparationTarget(8 * 60, '7–8 daqiqa')
    if name == 'chicken big' or name == 'chikin big':
        return PreparationTarget(11 * 60, '10–11 daqiqa')
    return None


def preparation_target_for_order(product_names):
    """Use the slowest tracked product as the target for a mixed order."""
    targets = [
        target
        for name in product_names
        if (target := preparation_target_for_name(name)) is not None
    ]
    if not targets:
        return None
    return max(targets, key=lambda target: target.maximum_seconds)


def classify_preparation(elapsed_seconds, target):
    """Green through target, yellow through 150%, then bright red."""
    elapsed_seconds = max(0, int(elapsed_seconds))
    if elapsed_seconds <= target.maximum_seconds:
        return GREEN
    if elapsed_seconds * 2 <= target.maximum_seconds * 3:
        return YELLOW
    return RED

