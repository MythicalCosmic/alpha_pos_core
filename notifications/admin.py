from django import forms
from django.contrib import admin
from django.db import models
from .models import (
    NotificationSettings, NotificationTemplate, NotificationLog,
    OrderNotificationDispatch,
)


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
    form = NotificationSettingsForm
    list_display = ('id', 'brand_name', 'is_enabled', 'timeout', 'bot_token_masked')

    @admin.display(description='Bot token')
    def bot_token_masked(self, obj):
        if not obj.bot_token:
            return '—'
        return f'****{obj.bot_token[-4:]}' if len(obj.bot_token) > 4 else '****'


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
