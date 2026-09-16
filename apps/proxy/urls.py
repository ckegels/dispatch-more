from django.urls import path, include

from apps.proxy import stats_views
from apps.proxy.live_proxy import overlap_views

app_name = 'proxy'

urlpatterns = [
    path('stats/', stats_views.combined_stats, name='combined_stats'),
    # Channel Switch Overlap: what the feature is doing, for its settings page
    path('overlap/', overlap_views.overlap_activity, name='overlap_activity'),
    path('ts/', include('apps.proxy.live_proxy.urls')),
    path('catchup/', include('apps.timeshift.urls')),
    path('vod/', include('apps.proxy.vod_proxy.urls')),
]
