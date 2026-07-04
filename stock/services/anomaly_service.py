"""Anomaly Watch — detectors that fire alerts when a metric trips.

A scan (run on a schedule via `manage.py scan_anomalies`, wired to Celery beat or
cron every ~15 min) runs each Detector over a window. New anomalies are deduped by
idempotency_key = sha1(detector, target, window) so the same condition does not
re-fire every scan, get a one-line AI explanation (best-effort), are broadcast to
the staff Telegram group (best-effort), and surface via the /ai/anomalies API.

Per-operator mute + quiet-hours live on AnomalySettings (the FE applies them to the
badge + browser notifications; Telegram is the always-on staff-group channel).
"""
import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from stock.models import Anomaly, AnomalySettings

logger = logging.getLogger(__name__)

_SEV = Anomaly.Severity

# Weekday names per language — RevenueDip uses these instead of English strftime('%A').
_WD = {
    'en': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
    'ru': ['понедельникам', 'вторникам', 'средам', 'четвергам', 'пятницам', 'субботам', 'воскресеньям'],
    'uz': ['dushanba', 'seshanba', 'chorshanba', 'payshanba', 'juma', 'shanba', 'yakshanba'],
}


def _parse_i18n(text):
    """Extract a {uz,ru,en} object from LLM output, defensively (missing langs
    backfilled from en; returns {} if nothing usable)."""
    import json
    s = (text or '').strip().strip('`')
    start, end = s.find('{'), s.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        d = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return {}
    if not isinstance(d, dict):
        return {}
    en = str(d.get('en') or '').strip()[:400]
    uz = str(d.get('uz') or '').strip()[:400]
    ru = str(d.get('ru') or '').strip()[:400]
    en = en or uz or ru
    return {'uz': uz or en, 'ru': ru or en, 'en': en} if en else {}


def _key(detector, target_kind, target_id, window_key):
    raw = f"{detector}|{target_kind}:{target_id}|{window_key}"
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


def _candidate(detector, severity, target_kind, target_id, window_key, message, deep_link):
    return {
        'detector': detector, 'severity': severity,
        'target_kind': target_kind, 'target_id': str(target_id),
        'window_key': str(window_key), 'message': message, 'deep_link': deep_link,
    }


# ── detectors ───────────────────────────────────────────────────────────────
class Detector:
    name = 'Detector'

    def scan(self, now):
        raise NotImplementedError


class LowStockCrossed(Detector):
    name = 'LowStockCrossed'

    def scan(self, now):
        from django.db.models import F, Q, Sum
        from base.services.business_day import business_date
        from stock.models import StockItem
        wk = business_date(now)
        out = []
        try:
            qs = (StockItem.objects.filter(is_deleted=False, is_active=True, reorder_point__gt=0)
                  .annotate(total_qty=Sum('stock_levels__quantity'))
                  .filter(Q(total_qty__lt=F('reorder_point')) | Q(total_qty__isnull=True)))
        except Exception:
            logger.exception('LowStockCrossed query failed')
            return out
        for it in qs[:50]:
            qty = it.total_qty if it.total_qty is not None else Decimal('0')
            rp = it.reorder_point or Decimal('0')
            sev = _SEV.CRITICAL if qty <= 0 else (_SEV.HIGH if rp and qty <= rp / 2 else _SEV.MEDIUM)
            out.append(_candidate(
                self.name, sev, 'stock_item', it.id, wk,
                {'en': f"{it.name} is low: {qty} left (reorder at {rp}).",
                 'ru': f"{it.name} заканчивается: осталось {qty} (точка заказа {rp}).",
                 'uz': f"{it.name} tugayapti: {qty} qoldi (buyurtma nuqtasi {rp})."},
                '/stock?filter=low'))
        return out


class RevenueDip(Detector):
    name = 'RevenueDip'
    DIP = Decimal('0.70')   # fire when the day is below 70% of its same-weekday average

    def _rev(self, d):
        from base.models import Order
        from base.services.business_day import range_window
        lo, hi = range_window(d, d)
        agg = (Order.objects.filter(is_deleted=False, is_paid=True,
                                    created_at__gte=lo, created_at__lt=hi)
               .exclude(status='CANCELED'))
        from django.db.models import Sum
        return agg.aggregate(t=Sum('total_amount'))['t'] or Decimal('0')

    def scan(self, now):
        from base.services.business_day import business_date
        d = business_date(now) - timedelta(days=1)   # the last full business day
        rev = self._rev(d)
        peers = [self._rev(d - timedelta(days=7 * k)) for k in range(1, 5)]
        peers = [p for p in peers if p > 0]
        if not peers:
            return []
        avg = sum(peers) / len(peers)
        if avg <= 0 or rev >= avg * self.DIP:
            return []
        pct = int((1 - (rev / avg)) * 100) if avg else 0
        sev = _SEV.HIGH if rev < avg * Decimal('0.5') else _SEV.MEDIUM
        wd = d.weekday()
        return [_candidate(
            self.name, sev, 'date', d.isoformat(), d.isoformat(),
            {'en': f"{d.isoformat()} revenue {int(rev):,} so'm — {pct}% below the "
                   f"{_WD['en'][wd]} average ({int(avg):,}).",
             'ru': f"Выручка за {d.isoformat()} — {int(rev):,} so'm, на {pct}% ниже "
                   f"среднего по {_WD['ru'][wd]} ({int(avg):,}).",
             'uz': f"{d.isoformat()} tushumi {int(rev):,} so'm — {_WD['uz'][wd]} kunlari "
                   f"o'rtachasidan {pct}% past ({int(avg):,})."},
            '/dashboard?range=7d')]


class CashierVoidBurst(Detector):
    name = 'CashierVoidBurst'

    def scan(self, now):
        from django.db.models import Count, Q
        from base.models import Order
        from base.services.business_day import business_date, day_window
        wk = business_date(now)
        lo, hi = day_window(wk)
        rows = (Order.objects.filter(is_deleted=False, cashier__isnull=False,
                                     created_at__gte=lo, created_at__lt=hi)
                .values('cashier_id', 'cashier__first_name', 'cashier__last_name')
                .annotate(total=Count('id'), cancels=Count('id', filter=Q(status='CANCELED'))))
        out = []
        for r in rows:
            total, cancels = r['total'] or 0, r['cancels'] or 0
            rate = cancels / total if total else 0
            if cancels >= 5 or (cancels >= 3 and total >= 5 and rate >= 0.3):
                name = f"{r['cashier__first_name'] or ''} {r['cashier__last_name'] or ''}".strip()
                sev = _SEV.HIGH if cancels >= 6 else _SEV.MEDIUM
                pct = int(rate * 100)
                out.append(_candidate(
                    self.name, sev, 'cashier', r['cashier_id'], wk,
                    {'en': f"{name or 'Cashier'} voided {cancels} of {total} orders today ({pct}%).",
                     'ru': f"{name or 'Кассир'} отменил(а) {cancels} из {total} заказов сегодня ({pct}%).",
                     'uz': f"{name or 'Kassir'} bugun {total} buyurtmadan {cancels} tasini bekor qildi ({pct}%)."},
                    f"/orders?cashier_id={r['cashier_id']}&status=CANCELED"))
        return out


class UnusualDiscount(Detector):
    name = 'UnusualDiscount'
    PCT = Decimal('40')   # flag orders discounted >= 40%

    def scan(self, now):
        from base.models import Order
        from base.services.business_day import business_date, day_window
        lo, hi = day_window(business_date(now))
        out = []
        rows = (Order.objects.filter(is_deleted=False, created_at__gte=lo, created_at__lt=hi)
                .exclude(status='CANCELED')
                .values('id', 'display_id', 'subtotal', 'discount_amount',
                        'discount_percent', 'cashier_id'))
        for r in rows:
            sub = r['subtotal'] or Decimal('0')
            damt = r['discount_amount'] or Decimal('0')
            dpct = r['discount_percent'] or Decimal('0')
            eff = dpct if dpct else ((damt / sub * 100) if sub else Decimal('0'))
            if eff >= self.PCT:
                sev = _SEV.HIGH if eff >= 70 else _SEV.MEDIUM
                out.append(_candidate(
                    self.name, sev, 'order', r['id'], r['id'],
                    {'en': f"Order #{r['display_id']} discounted {int(eff)}% ({int(damt):,} so'm off {int(sub):,}).",
                     'ru': f"Заказ #{r['display_id']} со скидкой {int(eff)}% ({int(damt):,} so'm из {int(sub):,}).",
                     'uz': f"#{r['display_id']} buyurtmaga {int(eff)}% chegirma ({int(sub):,} dan {int(damt):,} so'm)."},
                    f"/orders/{r['id']}"))
        return out


DETECTORS = [LowStockCrossed(), RevenueDip(), CashierVoidBurst(), UnusualDiscount()]


# ── scanner ─────────────────────────────────────────────────────────────────
class AnomalyScanner:

    @classmethod
    def scan(cls, now=None):
        now = now or timezone.now()
        created = []
        for det in DETECTORS:
            try:
                candidates = det.scan(now)
            except Exception:
                logger.exception('detector %s failed', det.name)
                continue
            for c in candidates:
                key = _key(c['detector'], c['target_kind'], c['target_id'], c['window_key'])
                if Anomaly.objects.filter(idempotency_key=key).exists():
                    continue
                msg = c['message']            # {uz,ru,en} dict
                expl = cls._explain(c)        # {uz,ru,en} dict, {} if unavailable
                try:
                    a = Anomaly.objects.create(
                        detector=c['detector'], severity=c['severity'],
                        target_kind=c['target_kind'], target_id=c['target_id'],
                        idempotency_key=key, deep_link=c['deep_link'],
                        message=msg['en'], message_i18n=msg,
                        ai_explanation=(expl.get('en', '') if expl else ''),
                        explanation_i18n=expl or {})
                except Exception:
                    # Unique race: another scan created it first — skip.
                    logger.debug('anomaly %s already exists (race)', key)
                    continue
                cls._deliver(a)
                created.append(a)
        return created

    @staticmethod
    def _explain(c):
        """AI explanation in {uz,ru,en}, best-effort ({} if the LLM is unavailable)."""
        try:
            from base.services.llm import call_ai, key_missing
            if key_missing():
                return {}
            m = c.get('message')
            seed = m.get('en', '') if isinstance(m, dict) else str(m or '')
            text, err = call_ai(
                "In ONE short sentence each, explain why this matters to a restaurant "
                "owner and the first thing to check. Return ONLY a JSON object with keys "
                "uz, ru, en (Uzbek, Russian, English), no markdown.\n" + seed,
                system='You output ONLY a valid JSON object {"uz":..,"ru":..,"en":..}. '
                       'No markdown, no prose.', max_tokens=400)
            return {} if (err or not text) else _parse_i18n(text)
        except Exception:
            logger.exception('anomaly explain failed')
            return {}

    @staticmethod
    def _deliver(anomaly):
        """Broadcast to the staff Telegram group (best-effort, never raises)."""
        try:
            from notifications.services.telegram_service import TelegramService
            lines = [f"⚠️ [{anomaly.severity.upper()}] {anomaly.message}"]
            if anomaly.ai_explanation:
                lines.append(anomaly.ai_explanation)
            TelegramService.send_message('\n'.join(lines))
        except Exception:
            logger.exception('anomaly telegram delivery failed')


# ── read/ack/settings API ────────────────────────────────────────────────────
class AnomalyService:

    @staticmethod
    def list_anomalies(since=None, unacked=False, limit=50):
        qs = Anomaly.objects.all()
        if since:
            qs = qs.filter(fired_at__gte=since)
        if unacked:
            qs = qs.filter(acked_at__isnull=True)
        limit = max(1, min(int(limit or 50), 200))
        rows = list(qs.order_by('-fired_at')[:limit])
        anomalies = [{
            'id': a.id, 'detector': a.detector, 'severity': a.severity,
            'fired_at': a.fired_at.isoformat() if a.fired_at else None,
            # {uz,ru,en}; old rows (no i18n) fall back to the English string.
            'message': a.message_i18n or {'uz': a.message, 'ru': a.message, 'en': a.message},
            'deep_link': a.deep_link,
            'ai_explanation': a.explanation_i18n or (
                {'uz': a.ai_explanation, 'ru': a.ai_explanation, 'en': a.ai_explanation}
                if a.ai_explanation else {}),
            'target_kind': a.target_kind, 'target_id': a.target_id,
            'acked': a.acked_at is not None,
        } for a in rows]
        cursor = anomalies[-1]['fired_at'] if anomalies else None
        return {'anomalies': anomalies, 'cursor': cursor}

    @staticmethod
    def ack(anomaly_id, user_id):
        a = Anomaly.objects.filter(id=anomaly_id).first()
        if not a:
            return False
        if a.acked_at is None:
            a.acked_by = user_id
            a.acked_at = timezone.now()
            a.save(update_fields=['acked_by', 'acked_at'])
        return True

    @staticmethod
    def _serialize_settings(s):
        return {
            'muted_detectors': s.muted_detectors or [],
            'quiet_start': s.quiet_start.strftime('%H:%M') if s.quiet_start else None,
            'quiet_end': s.quiet_end.strftime('%H:%M') if s.quiet_end else None,
            'quiet_tz': s.quiet_tz or '',
            'detectors': [d.name for d in DETECTORS],   # so the FE can list toggles
        }

    @classmethod
    def get_settings(cls, user_id):
        s, _ = AnomalySettings.objects.get_or_create(user_id=user_id)
        return cls._serialize_settings(s)

    @classmethod
    def update_settings(cls, user_id, **kwargs):
        from datetime import datetime as _dt
        s, _ = AnomalySettings.objects.get_or_create(user_id=user_id)
        if 'muted_detectors' in kwargs and isinstance(kwargs['muted_detectors'], list):
            s.muted_detectors = [str(x) for x in kwargs['muted_detectors']]
        for fld in ('quiet_start', 'quiet_end'):
            if fld in kwargs:
                v = kwargs[fld]
                if not v:
                    setattr(s, fld, None)
                else:
                    try:
                        setattr(s, fld, _dt.strptime(str(v).strip(), '%H:%M').time())
                    except (ValueError, TypeError):
                        pass
        if 'quiet_tz' in kwargs:
            s.quiet_tz = str(kwargs['quiet_tz'] or '')[:64]
        s.save()
        return cls._serialize_settings(s)


__all__ = ['AnomalyScanner', 'AnomalyService', 'DETECTORS']
