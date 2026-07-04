"""AI Morning Briefing — compose (and cache) a once-per-business-day digest.

ONE Anthropic call per (operator, business day): gather the day's overview +
inventory health + menu engineering, ask the model for 5 prioritized bullets, cache
them on AIBriefing. If the LLM is unavailable or returns junk, fall back to a
templated, data-driven briefing so the card always renders.
"""
import json
import logging
from datetime import timedelta

from django.utils import timezone

from stock.models import AIBriefing

logger = logging.getLogger(__name__)

VALID_HOURS = 16  # the briefing stays "fresh" through the operating day

_BRIEFING_SYSTEM = (
    "You are the operator's morning analyst. You output ONLY a valid JSON array — no "
    "prose, no markdown fences. Each element is an object with EXACTLY these keys: "
    "icon, title, body, deep_link, ai_seed_prompt. `title` and `body` are each an "
    "OBJECT with EXACTLY three string keys: \"uz\" (Uzbek, Latin script), \"ru\" "
    "(Russian), \"en\" (English) — the SAME message written in each language, never "
    "left blank. Money is integer so'm (keep the digits identical across languages; "
    "only translate the words). Keep each title <= 7 words and each body <= 25 words "
    "in every language. icon, deep_link and ai_seed_prompt stay plain strings "
    "(ai_seed_prompt in English)."
)


class AIBriefingService:

    @classmethod
    def get_or_generate(cls, user_id, location_id=None):
        from base.services.business_day import business_date
        bdate = business_date()
        b, created = AIBriefing.objects.get_or_create(
            user_id=user_id, business_date=bdate, defaults={'bullets': []})
        if created or not b.bullets:
            b.bullets = cls._compose(location_id)
            b.valid_until = timezone.now() + timedelta(hours=VALID_HOURS)
            b.save(update_fields=['bullets', 'valid_until'])
        return cls._serialize(b)

    @classmethod
    def dismiss(cls, user_id):
        from base.services.business_day import business_date
        AIBriefing.objects.filter(
            user_id=user_id, business_date=business_date()).update(dismissed=True)
        return True

    @staticmethod
    def _serialize(b):
        return {
            'id': b.id,
            'generated_at': b.generated_at.isoformat() if b.generated_at else None,
            'valid_until': b.valid_until.isoformat() if b.valid_until else None,
            'dismissed': b.dismissed,
            'bullets': b.bullets or [],
        }

    # ── snapshot + LLM composition ──────────────────────────────────────────
    @classmethod
    def _snapshot(cls, location_id=None):
        from stock.services.ai_assistant_service import AIStockAssistant
        snap = {}
        try:
            from stock.services.ai_tools_service import AIToolbox
            snap['overview'] = AIToolbox.execute('get_overview', {}, location_id)
        except Exception:
            logger.exception('briefing: overview failed')
        for key, fn in (
            ('inventory_health', AIStockAssistant._get_inventory_health),
            ('menu_engineering', AIStockAssistant._get_menu_engineering),
            ('sales', AIStockAssistant._get_sales_data),
        ):
            try:
                snap[key] = fn()
            except Exception:
                logger.exception('briefing: %s failed', key)
        return snap

    @classmethod
    def _compose(cls, location_id=None):
        snap = cls._snapshot(location_id)
        try:
            from base.services.llm import call_ai, key_missing
            if not key_missing():
                prompt = (
                    "Compose EXACTLY 5 morning-briefing bullets (priority order) for a "
                    "restaurant owner from this snapshot. Topics, one each: (1) yesterday "
                    "revenue vs the same weekday's average, (2) the biggest mover product, "
                    "(3) an item about to run out, (4) the worst cashier shift, (5) one "
                    "menu-engineering action. icon is one of "
                    "[revenue, trend, stock, staff, menu]. deep_link is an app route like "
                    "\"/dashboard?range=7d\" or \"/stock?filter=low\". ai_seed_prompt is a "
                    "question that opens an AI thread to dig deeper. Each bullet's `title` "
                    "and `body` MUST be an object {\"uz\":..., \"ru\":..., \"en\":...} with "
                    "all three languages filled. Return ONLY the JSON array.\n\nSNAPSHOT:\n"
                    + json.dumps(snap, default=str, ensure_ascii=False)[:12000]
                )
                text, err = call_ai(prompt, system=_BRIEFING_SYSTEM, max_tokens=2200)
                if not err and text:
                    bullets = cls._parse_bullets(text)
                    if bullets:
                        return bullets[:5]
        except Exception:
            logger.exception('briefing: LLM composition failed; using fallback')
        return cls._fallback(snap)

    @staticmethod
    def _langify(val, limit):
        """Coerce a title/body value into {'uz','ru','en'}. Accepts a dict (already
        trilingual — backfill any missing language from English) or a plain string
        (treated as all three)."""
        if isinstance(val, dict):
            en = str(val.get('en') or '').strip()[:limit]
            uz = str(val.get('uz') or '').strip()[:limit]
            ru = str(val.get('ru') or '').strip()[:limit]
            en = en or uz or ru
            return {'uz': uz or en, 'ru': ru or en, 'en': en}
        s = str(val or '').strip()[:limit]
        return {'uz': s, 'ru': s, 'en': s}

    @classmethod
    def _parse_bullets(cls, text):
        """Pull a JSON array of bullets out of the model output, defensively. title
        and body may be a string or a {uz,ru,en} object; both normalize to a flat
        English title/body (backward compat) PLUS title_i18n/body_i18n {uz,ru,en}."""
        s = (text or '').strip()
        if s.startswith('```'):
            s = s.strip('`')
            s = s[s.find('['):] if '[' in s else s
        start, end = s.find('['), s.rfind(']')
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(s[start:end + 1])
        except (ValueError, TypeError):
            return []
        out = []
        for it in data if isinstance(data, list) else []:
            if not isinstance(it, dict):
                continue
            title = cls._langify(it.get('title'), 120)
            body = cls._langify(it.get('body'), 400)
            out.append({
                'icon': str(it.get('icon') or 'trend')[:24],
                'title': title['en'],            # flat English — backward compat
                'body': body['en'],              # flat English — backward compat
                'title_i18n': title,             # {uz,ru,en}
                'body_i18n': body,               # {uz,ru,en}
                'deep_link': str(it.get('deep_link') or '')[:255],
                'ai_seed_prompt': str(it.get('ai_seed_prompt') or '')[:400],
            })
        return [b for b in out if b['title'] or b['body']]

    @classmethod
    def _mk(cls, icon, title, body, deep_link, ai_seed_prompt):
        """Build a fallback bullet with flat English + {uz,ru,en} for title/body."""
        t = cls._langify(title, 120)
        b = cls._langify(body, 400)
        return {'icon': icon, 'title': t['en'], 'body': b['en'],
                'title_i18n': t, 'body_i18n': b,
                'deep_link': deep_link, 'ai_seed_prompt': ai_seed_prompt}

    @classmethod
    def _fallback(cls, snap):
        """Templated, data-driven bullets when the LLM can't compose — always
        renders something useful, in uz/ru/en."""
        bullets = []
        inv = snap.get('inventory_health') or {}
        summary = inv.get('summary') or {}
        low = summary.get('dead_stock_count')
        dead = (inv.get('dead_stock') or [])
        sales = snap.get('sales') or {}
        today = sales.get('today') or {}
        top = (sales.get('top_products_today') or sales.get('top_products_30_days') or [])
        menu = snap.get('menu_engineering') or {}
        msummary = menu.get('summary') or {}

        if today.get('total_revenue_uzs') is not None:
            rev = int(today.get('total_revenue_uzs') or 0)
            cnt = today.get('count', 0)
            bullets.append(cls._mk(
                icon='revenue',
                title={'en': "Today's revenue so far", 'ru': 'Выручка за сегодня',
                       'uz': 'Bugungi tushum'},
                body={'en': f"{rev:,} so'm across {cnt} orders.",
                      'ru': f"{rev:,} so'm, заказов: {cnt}.",
                      'uz': f"{rev:,} so'm, {cnt} ta buyurtma."},
                deep_link='/dashboard?range=today',
                ai_seed_prompt='How does today compare to the same weekday average?'))
        if top:
            name = top[0].get('name', '—')
            rev = int(top[0].get('revenue_uzs') or 0)
            bullets.append(cls._mk(
                icon='trend',
                title={'en': f"Top seller: {name}", 'ru': f"Лидер продаж: {name}",
                       'uz': f"Eng ko'p sotilgan: {name}"},
                body={'en': f"{rev:,} so'm so far.", 'ru': f"{rev:,} so'm на данный момент.",
                      'uz': f"Hozircha {rev:,} so'm."},
                deep_link='/dashboard/products',
                ai_seed_prompt=f"Why is {name} selling well?"))
        if dead:
            name = dead[0].get('name', 'An item')
            days = dead[0].get('days_since_last_movement', '?')
            bullets.append(cls._mk(
                icon='stock',
                title={'en': 'Dead / slow stock', 'ru': 'Залежавшийся товар',
                       'uz': 'Sotilmayotgan mahsulot'},
                body={'en': f"{name} hasn't moved in {days} days.",
                      'ru': f"{name} не продаётся уже {days} дн.",
                      'uz': f"{name} {days} kundan beri sotilmagan."},
                deep_link='/stock?filter=dead',
                ai_seed_prompt='Which items should I discount or discontinue?'))
        elif low:
            bullets.append(cls._mk(
                icon='stock',
                title={'en': 'Inventory needs attention', 'ru': 'Требуется проверка склада',
                       'uz': "Ombor e'tibor talab qiladi"},
                body={'en': f"{low} item(s) flagged in inventory health.",
                      'ru': f"{low} позиц. отмечены в проверке склада.",
                      'uz': f"{low} ta mahsulot ombor tekshiruvida belgilangan."},
                deep_link='/stock',
                ai_seed_prompt='What stock is running low?'))
        if msummary.get('dogs'):
            n = msummary['dogs']
            bullets.append(cls._mk(
                icon='menu',
                title={'en': 'Menu action: review the "Dogs"',
                       'ru': 'Меню: пересмотреть «Аутсайдеров»',
                       'uz': 'Menyu: "Autsayderlar"ni ko\'rib chiqing'},
                body={'en': f"{n} low-popularity, low-margin item(s) to rethink.",
                      'ru': f"{n} непопул., низкомарж. позиц. на пересмотр.",
                      'uz': f"{n} ta kam ommabop, kam foydali mahsulotni qayta ko'rib chiqing."},
                deep_link='/dashboard/products',
                ai_seed_prompt='Show me the menu-engineering Dogs and what to do.'))
        return bullets[:5]


__all__ = ['AIBriefingService']
