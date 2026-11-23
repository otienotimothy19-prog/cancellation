from django.contrib import admin

from .models import CancellationCase, CaseDocument, CaseEmail, CaseEvent, Cover, Insurer


@admin.register(Insurer)
class InsurerAdmin(admin.ModelAdmin):
    list_display = ("name", "cancellation_email", "sla_hours", "requires_signed_form", "active")
    list_filter = ("active", "requires_signed_form")
    search_fields = ("name", "cancellation_email")


@admin.register(Cover)
class CoverAdmin(admin.ModelAdmin):
    list_display = ("policy_number", "client_name", "insurer", "premium", "start_date", "end_date", "status")
    list_filter = ("status", "insurer")
    search_fields = ("policy_number", "client_name", "asset_reference")
    readonly_fields = ("status",)  # only the cancellation workflow changes cover status


class ReadOnlyInline(admin.TabularInline):
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class EventInline(ReadOnlyInline):
    model = CaseEvent
    fields = ("timestamp", "actor", "action", "from_status", "to_status", "note")
    readonly_fields = fields


class EmailInline(ReadOnlyInline):
    model = CaseEmail
    fk_name = "case"
    fields = ("received_at", "direction", "from_email", "subject", "classification", "matched_by")
    readonly_fields = fields


@admin.register(CancellationCase)
class CancellationCaseAdmin(admin.ModelAdmin):
    """Read-only in admin: cases are created, sent and reviewed through the app so every step is logged."""

    list_display = ("reference", "cover", "reason_code", "effective_date", "status", "is_stale", "created_at")
    list_filter = ("status", "is_stale", "reason_code")
    search_fields = ("cover__policy_number", "cover__client_name")
    inlines = [EmailInline, EventInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CaseEvent)
class CaseEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "case", "actor", "action", "from_status", "to_status")
    list_filter = ("action",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CaseEmail)
class CaseEmailAdmin(CaseEventAdmin):
    list_display = ("received_at", "direction", "case", "from_email", "subject", "classification", "matched_by")
    list_filter = ("direction", "classification", "matched_by")
    search_fields = ("subject", "from_email", "message_id")


@admin.register(CaseDocument)
class CaseDocumentAdmin(CaseEventAdmin):
    list_display = ("uploaded_at", "case", "type", "original_name", "source", "size")
    list_filter = ("type", "source")
