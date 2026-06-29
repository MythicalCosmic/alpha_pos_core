"""AI Morning Briefing — a once-per-business-day proactive digest.

One row per (operator, business day): the 5 bullets an LLM composed overnight from
the day's overview + inventory + menu-engineering snapshot. Cached so only ONE
Anthropic call is spent per business per morning; `dismissed` collapses the card for
the rest of the business day. Non-synced (back-office, per-operator, local meaning).
"""
from django.db import models


class AIBriefing(models.Model):
    user_id = models.IntegerField(db_index=True)
    business_date = models.DateField(db_index=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    # [{icon, title, body, deep_link, ai_seed_prompt}, ...] — priority order.
    bullets = models.JSONField(default=list)
    dismissed = models.BooleanField(default=False)

    class Meta:
        db_table = 'ai_briefing'
        unique_together = (('user_id', 'business_date'),)
        ordering = ['-business_date']

    def __str__(self):
        return f"AIBriefing<u{self.user_id} {self.business_date} bullets={len(self.bullets or [])}>"
