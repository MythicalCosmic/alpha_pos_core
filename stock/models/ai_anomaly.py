"""Anomaly Watch — detectors fire alerts when a metric trips.

`Anomaly` is one fired alert (deduped via idempotency_key = sha1(detector, target,
window) so the same z-score doesn't re-fire every scan). `AnomalySettings` holds a
per-operator mute list + quiet hours. Both non-synced (back-office, per-operator).
"""
from django.db import models


class Anomaly(models.Model):
    class Severity(models.TextChoices):
        LOW = 'low', 'Low'
        MEDIUM = 'medium', 'Medium'
        HIGH = 'high', 'High'
        CRITICAL = 'critical', 'Critical'

    detector = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.MEDIUM)
    fired_at = models.DateTimeField(auto_now_add=True, db_index=True)
    target_kind = models.CharField(max_length=32, blank=True, default='')   # product / cashier / ...
    target_id = models.CharField(max_length=64, blank=True, default='')
    # sha1(detector, target, window) — a UNIQUE row per (anomaly, window) so the
    # same condition doesn't re-fire on every 15-min scan.
    idempotency_key = models.CharField(max_length=64, unique=True)
    message = models.TextField(blank=True, default='')
    deep_link = models.CharField(max_length=255, blank=True, default='')
    ai_explanation = models.TextField(blank=True, default='')
    acked_by = models.IntegerField(null=True, blank=True)
    acked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'ai_anomaly'
        ordering = ['-fired_at']

    def __str__(self):
        return f"Anomaly<{self.detector}/{self.severity} {self.target_kind}:{self.target_id}>"


class AnomalySettings(models.Model):
    """Per-operator anomaly preferences: muted detector list + quiet hours."""
    user_id = models.IntegerField(unique=True, db_index=True)
    muted_detectors = models.JSONField(default=list)   # ['RevenueDip', ...]
    quiet_start = models.TimeField(null=True, blank=True)
    quiet_end = models.TimeField(null=True, blank=True)
    quiet_tz = models.CharField(max_length=64, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'ai_anomaly_settings'

    def __str__(self):
        return f"AnomalySettings<u{self.user_id} muted={self.muted_detectors}>"
