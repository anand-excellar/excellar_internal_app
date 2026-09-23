from django.contrib import admin

from catalog.models import Dashboard


@admin.register(Dashboard)
class DashboardAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "upstream", "enabled", "order")
    list_editable = ("enabled", "order")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name", "slug", "upstream")