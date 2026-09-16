from django.urls import path, include

from apps.proxy import stats_views
from apps.proxy.live_proxy import (
    diagnostics_views,
    hdhr_tuner_views,
    media_server_tuner_views,
    media_server_views,
)

app_name = 'proxy'

urlpatterns = [
    path('stats/', stats_views.combined_stats, name='combined_stats'),
    # Diagnostics: channel starts and what Channel Switch Overlap is doing
    path('diagnostics/', diagnostics_views.diagnostics, name='live_diagnostics'),
    # Media Servers: the servers themselves, for the settings tab
    path('media-servers/', media_server_views.media_server_list, name='media_servers'),
    path(
        'media-servers/tuners/',
        media_server_tuner_views.media_server_tuners,
        name='media_server_tuners',
    ),
    # An HDHomeRun whose tuner count comes from the address (see hdhr_tuner_views)
    path(
        'hdhr/<str:channel_profile>/tuners/<int:tuner_count>/<str:document>',
        hdhr_tuner_views.hdhr_document,
        name='hdhr_with_tuners',
    ),
    path(
        'hdhr/<str:channel_profile>/output_profile/<int:output_profile_id>/'
        'tuners/<int:tuner_count>/<str:document>',
        hdhr_tuner_views.hdhr_document,
        name='hdhr_with_output_and_tuners',
    ),
    path('ts/', include('apps.proxy.live_proxy.urls')),
    path('catchup/', include('apps.timeshift.urls')),
    path('vod/', include('apps.proxy.vod_proxy.urls')),
]
