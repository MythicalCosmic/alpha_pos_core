"""Run the Anomaly Watch detectors once and fire any NEW alerts.

Schedule this every ~15 minutes via Celery beat or cron, e.g.:
    */15 * * * *  python manage.py scan_anomalies
New anomalies are deduped (idempotency_key), get a one-line AI explanation, and are
broadcast to the staff Telegram group; the FE polls /api/admins/ai/anomalies.
"""
from django.core.management.base import BaseCommand

from stock.services.anomaly_service import AnomalyScanner


class Command(BaseCommand):
    help = "Scan for anomalies and fire new alerts (schedule every ~15 min)."

    def handle(self, *args, **options):
        created = AnomalyScanner.scan()
        self.stdout.write(self.style.SUCCESS(
            f"anomaly scan complete: {len(created)} new alert(s)"))
        for a in created:
            self.stdout.write(f"  [{a.severity}] {a.detector}: {a.message}")
