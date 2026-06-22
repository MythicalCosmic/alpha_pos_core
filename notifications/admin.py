from django import forms
from django.contrib import admin, messages
from django.db import models
from .models import (
    NotificationSettings, NotificationTemplate, NotificationLog,
    OrderNotificationDispatch, NotificationChat,
)

_TEST_TEXT = ("\U0001F514 <b>Test bildirishnoma</b>\n"
             "Alpha POS Telegram bildirishnomalari ishlayapti. ✅")


def _send_test(modeladmin, request, chat_ids):
    from notifications.services.telegram_service import TelegramService
    chat_ids = [str(c) for c in chat_ids]
    if not chat_ids:
        modeladmin.message_user(request, 'No chats selected.', level=messages.WARNING)
        return
    ok, err = TelegramService.send_message(_TEST_TEXT, chat_ids=chat_ids)
    if ok:
        modeladmin.message_user(request, f'Test sent to {len(chat_ids)} chat(s).')
    else:
        modeladmin.message_user(request, f'Test failed: {err}', level=messages.ERROR)


class NotificationSettingsForm(forms.ModelForm):
    # Mask the bot token in the form: existing value isn't rendered back to
    # the page, and submitting an empty token preserves the saved value.
    bot_token = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text='Leave blank to keep current token.',
    )

    class Meta:
        model = NotificationSettings
        fields = '__all__'

    def clean_bot_token(self):
        new = self.cleaned_data.get('bot_token') or ''
        if not new and self.instance and self.instance.pk:
            return self.instance.bot_token
        return new


@admin.register(NotificationSettings)
class NotificationSettingsAdmin(admin.ModelAdmin):
    """Global Telegram settings: bot token, brand, master on/off. The recipient
    chats + per-category routing live under 'Notification chats' (this row's
    chat_ids/chat_routing are derived from there and shown read-only)."""
    form = NotificationSettingsForm
    list_display = ('id', 'brand_name', 'is_enabled', 'timeout', 'bot_token_masked')
    readonly_fields = ('chat_ids', 'chat_routing', 'created_at', 'updated_at')
    actions = ['send_test_to_all']

    @admin.display(description='Bot token')
    def bot_token_masked(self, obj):
        if not obj.bot_token:
            return '—'
        return f'****{obj.bot_token[-4:]}' if len(obj.bot_token) > 4 else '****'

    @admin.action(description='Send a TEST notification to ALL enabled chats')
    def send_test_to_all(self, request, queryset):
        _send_test(self, request, NotificationSettings.load().chat_ids or [])


@admin.register(NotificationChat)
class NotificationChatAdmin(admin.ModelAdmin):
    """Add a Telegram chat id + label, and tick which message categories it
    receives — inline, no JSON. Use 'Send test' to verify delivery."""
    list_display = ('chat_id', 'label', 'is_enabled', 'recv_orders', 'recv_shifts',
                    'recv_contracts', 'recv_documents', 'recv_system')
    list_editable = ('label', 'is_enabled', 'recv_orders', 'recv_shifts',
                     'recv_contracts', 'recv_documents', 'recv_system')
    search_fields = ('chat_id', 'label')
    actions = ['send_test']

    @admin.action(description='Send a TEST notification to the selected chat(s)')
    def send_test(self, request, queryset):
        _send_test(self, request, list(queryset.values_list('chat_id', flat=True)))


@admin.register(NotificationTemplate)
class NotificationTemplateAdmin(admin.ModelAdmin):
    """Edit the Telegram message wording here. `description` lists the available
    {placeholders}; `notification_type` is the code key and is locked on edit."""
    list_display = ('notification_type', 'name', 'is_enabled', 'language', 'updated_at')
    list_filter = ('is_enabled', 'language')
    list_editable = ('is_enabled',)
    search_fields = ('notification_type', 'name', 'template_text')
    fields = ('notification_type', 'name', 'is_enabled', 'language',
              'description', 'template_text', 'created_at', 'updated_at')
    formfield_overrides = {
        models.TextField: {'widget': forms.Textarea(
            attrs={'rows': 16, 'cols': 72, 'style': 'font-family:monospace'})},
    }

    def get_readonly_fields(self, request, obj=None):
        base = ('created_at', 'updated_at')
        # Lock the code key when editing an existing template (renaming it would
        # break the lookup in the send path); it's editable only when adding.
        return base + ('notification_type',) if obj else base


@admin.register(OrderNotificationDispatch)
class OrderNotificationDispatchAdmin(admin.ModelAdmin):
    list_display = ('order_id', 'new_sent', 'ready_sent', 'paid_sent',
                    'cancelled_sent', 'updated_at')
    search_fields = ('order_id',)
    list_filter = ('new_sent', 'ready_sent')
    readonly_fields = ('order_id', 'new_sent', 'ready_sent', 'paid_sent',
                       'cancelled_sent', 'new_message_ids', 'created_at', 'updated_at')


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'notification_type', 'recipient', 'status', 'created_at')
    list_filter = ('status', 'notification_type')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at',)
