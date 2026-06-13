from django.contrib import admin

from licensing.models import License, LicenseEvent


@admin.register(License)
class LicenseAdmin(admin.ModelAdmin):
    """Read-only view of the singleton row — the operator should not be
    editing license state through Django admin. Use the setup wizard for
    initial registration; everything else flows from the control center."""

    list_display = ('org_name', 'email', 'status', 'expires_at', 'last_heartbeat_at')
    readonly_fields = (
        'org_name', 'email', 'status', 'expires_at',
        'last_heartbeat_at', 'last_server_now', 'last_message',
        'fingerprint', 'registered_at', 'created_at', 'updated_at',
    )
    fields = readonly_fields  # hide key_encrypted entirely

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LicenseEvent)
class LicenseEventAdmin(admin.ModelAdmin):
    list_display = ('action', 'created_at')
    list_filter = ('action',)
    readonly_fields = ('action', 'detail', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
