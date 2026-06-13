from django import forms
from django.contrib import admin
from .models import NotificationSettings, NotificationTemplate, NotificationLog


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
    list_display = ('id', 'notification_type', 'name', 'is_enabled', 'language')
    list_filter = ('is_enabled', 'language')


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'notification_type', 'recipient', 'status', 'created_at')
    list_filter = ('status', 'notification_type')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at',)
