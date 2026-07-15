"""Canonical phone handling shared by POS, cloud, and bot order paths."""


UZ_COUNTRY_CODE = "998"
UZ_NATIONAL_DIGITS = 9
UZ_CANONICAL_DIGITS = len(UZ_COUNTRY_CODE) + UZ_NATIONAL_DIGITS


def normalize_uz_phone(value):
    """Return a digits-only Uzbekistan phone key.

    The common local/international spellings converge to ``998XXXXXXXXX``:
    ``90 123 45 67``, ``0 90 123 45 67``, ``+998 90 123 45 67`` and
    ``00998...``. Short legacy identifiers are kept as digits rather than
    erased; callers requiring a dialable number can additionally validate
    :func:`is_canonical_uz_phone`.
    """
    if value is None:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == UZ_NATIONAL_DIGITS:
        digits = UZ_COUNTRY_CODE + digits
    elif len(digits) == UZ_NATIONAL_DIGITS + 1 and digits.startswith("0"):
        digits = UZ_COUNTRY_CODE + digits[1:]
    return digits[:20]


def is_canonical_uz_phone(value):
    normalized = normalize_uz_phone(value)
    return (
        len(normalized) == UZ_CANONICAL_DIGITS
        and normalized.startswith(UZ_COUNTRY_CODE)
    )

