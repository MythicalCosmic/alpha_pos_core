from datetime import datetime, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from stock.services.base_service import generate_number, get_date_range
from stock.services.batch_service import StockBatchService


@override_settings(TIME_ZONE="Asia/Tashkent", USE_TZ=True)
class LocalBusinessDateTests(TestCase):
    """Calendar-day decisions must follow the restaurant's local timezone."""

    def setUp(self):
        super().setUp()
        self.utc_after_tashkent_midnight = datetime(
            2026, 7, 13, 20, 30, tzinfo=datetime_timezone.utc
        )
        timezone.activate(ZoneInfo("Asia/Tashkent"))

    def tearDown(self):
        timezone.deactivate()
        super().tearDown()

    @patch("django.utils.timezone.now")
    def test_date_ranges_use_tashkent_date_after_local_midnight(self, mocked_now):
        mocked_now.return_value = self.utc_after_tashkent_midnight

        start, end = get_date_range("today")

        self.assertEqual(start.isoformat(), "2026-07-14")
        self.assertEqual(end, start)

    @patch("base.services.sequence._max_existing_seq", return_value=0)
    @patch("django.utils.timezone.now")
    def test_document_number_uses_local_calendar_date(
        self, mocked_now, _mocked_existing_sequence
    ):
        mocked_now.return_value = self.utc_after_tashkent_midnight

        number = generate_number("TZTEST", object)

        self.assertEqual(number, "TZTEST-20260714-0001")

    @patch("django.utils.timezone.now")
    def test_expiry_status_uses_local_calendar_date(self, mocked_now):
        mocked_now.return_value = self.utc_after_tashkent_midnight
        today_batch = SimpleNamespace(expiry_date=timezone.localdate())

        self.assertEqual(StockBatchService._days_until_expiry(today_batch), 0)
        self.assertFalse(StockBatchService._is_expired(today_batch))
