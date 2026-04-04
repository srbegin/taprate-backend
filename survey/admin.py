from django.contrib import admin
from .models import Organization, User, Survey, Incentive, Location, SurveyResponse, Alert

admin.site.register(Organization)
admin.site.register(User)
admin.site.register(Survey)
admin.site.register(Incentive)
admin.site.register(Location)
admin.site.register(SurveyResponse)
admin.site.register(Alert)

# from django.contrib import admin
# from django.db.models import Avg, Count
# from django.utils.html import format_html
# from .models import Property, Location, SurveyResponse, Alert


# @admin.register(Property)
# class PropertyAdmin(admin.ModelAdmin):
#     list_display = ['name', 'address', 'location_count', 'created_at']

#     def location_count(self, obj):
#         return obj.locations.filter(active=True).count()
#     location_count.short_description = 'Active Locations'


# @admin.register(Location)
# class LocationAdmin(admin.ModelAdmin):
#     list_display = ['name', 'property', 'floor', 'active', 'avg_rating_7d', 'nfc_url_display']
#     list_filter = ['active', 'property']
#     actions = ['export_nfc_urls']

#     def avg_rating_7d(self, obj):
#         data = obj.average_rating(days=7)
#         avg = data.get('avg')
#         if avg is None:
#             return '—'
#         color = '#e85252' if avg < 3 else '#4ac8a0'
#         return format_html('<span style="color:{}">{:.1f} ★</span>', color, avg)
#     avg_rating_7d.short_description = 'Avg (7d)'

#     def nfc_url_display(self, obj):
#         return format_html('<a href="{}" target="_blank">🔗 NFC URL</a>', obj.nfc_url)
#     nfc_url_display.short_description = 'NFC Tag URL'

#     def export_nfc_urls(self, request, queryset):
#         import csv
#         from django.http import HttpResponse
#         response = HttpResponse(content_type='text/csv')
#         response['Content-Disposition'] = 'attachment; filename="nfc_urls.csv"'
#         writer = csv.writer(response)
#         writer.writerow(['Property', 'Location', 'Floor', 'NFC URL', 'QR Fallback'])
#         for loc in queryset:
#             writer.writerow([loc.property.name, loc.name, loc.floor, loc.nfc_url, loc.nfc_url])
#         return response
#     export_nfc_urls.short_description = 'Export NFC URLs (CSV)'


# @admin.register(SurveyResponse)
# class SurveyResponseAdmin(admin.ModelAdmin):
#     list_display = ['location', 'rating_display', 'created_at', 'ip_hash']
#     list_filter = ['rating', 'location__property', 'created_at']
#     date_hierarchy = 'created_at'
#     readonly_fields = ['id', 'ip_hash', 'device_hash', 'user_agent']

#     def rating_display(self, obj):
#         colors = {1: '#e85252', 2: '#e8894a', 3: '#e8c94a', 4: '#7ec87a', 5: '#4ac8a0'}
#         return format_html(
#             '<span style="color:{};font-weight:bold">{} {}</span>',
#             colors[obj.rating], '★' * obj.rating, obj.rating
#         )
#     rating_display.short_description = 'Rating'


# @admin.register(Alert)
# class AlertAdmin(admin.ModelAdmin):
#     list_display = ['location', 'rating', 'status', 'channels_notified', 'created_at', 'resolved_at']
#     list_filter = ['status', 'location__property']
#     actions = ['mark_resolved']

#     def mark_resolved(self, request, queryset):
#         queryset.update(status='resolved', resolved_at=timezone.now())
#     mark_resolved.short_description = 'Mark as Resolved'
