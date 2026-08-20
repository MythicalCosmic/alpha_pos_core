from base.services.phone import is_canonical_uz_phone, normalize_uz_phone


def test_phone_normalization_accepts_common_ascii_formats():
    assert normalize_uz_phone('+998 (90) 123-45-67') == '998901234567'
    assert normalize_uz_phone('90 123 45 67') == '998901234567'
    assert is_canonical_uz_phone('998901234567') is True
    assert is_canonical_uz_phone('+998 (90) 123-45-67') is False


def test_phone_normalization_rejects_unicode_digit_lookalikes():
    assert normalize_uz_phone('998۹۰۱۲۳۴۵۶۷') == '998'
    assert normalize_uz_phone('998９０１２３４５６７') == '998'
    assert is_canonical_uz_phone('998۹۰۱۲۳۴۵۶۷') is False
