from django.urls import path, include

from apps.proxy import stats_views
from apps.proxy.live_proxy import diagnostics_views

app_name = 'proxy'

urlpatterns = [
    path('stats/', stats_views.combined_stats, name='combined_stats'),
    # Diagnostics: channel starts and what Channel Switch Overlap is doing
    path('diagnostics/', diagnostics_views.diagnostics, name='live_diagnostics'),
    path('ts/', include('apps.proxy.live_proxy.urls')),
    path('catchup/', include('apps.timeshift.urls')),
    path('vod/', include('apps.proxy.vod_proxy.urls')),
]
