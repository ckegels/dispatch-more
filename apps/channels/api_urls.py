from django.urls import path, include
from rest_framework.routers import DefaultRouter

from . import channel_manager_views, logo_library_views, stream_check_views
from .api_views import (
    StreamViewSet,
    ChannelViewSet,
    ChannelGroupViewSet,
    BulkDeleteStreamsAPIView,
    BulkDeleteChannelsAPIView,
    BulkDeleteLogosAPIView,
    CleanupUnusedLogosAPIView,
    LogoViewSet,
    ChannelProfileViewSet,
    UpdateChannelMembershipAPIView,
    BulkUpdateChannelMembershipAPIView,
    RecordingViewSet,
    RECORDING_PLAYBACK_AUTHENTICATORS,
    RecurringRecordingRuleViewSet,
    GetChannelStreamsAPIView,
    GetChannelStreamStatsAPIView,
    SeriesRulesAPIView,
    SeriesRulePreviewAPIView,
    EvaluateSeriesRulesAPIView,
    BulkRemoveSeriesRecordingsAPIView,
    BulkDeleteUpcomingRecordingsAPIView,
    ComskipConfigAPIView,
)

app_name = 'channels'  # for DRF routing

router = DefaultRouter()
router.register(r'streams', StreamViewSet, basename='stream')
router.register(r'groups', ChannelGroupViewSet, basename='channel-group')
router.register(r'channels', ChannelViewSet, basename='channel')
router.register(r'logos', LogoViewSet, basename='logo')
router.register(r'profiles', ChannelProfileViewSet, basename='profile')
router.register(r'recordings', RecordingViewSet, basename='recording')
router.register(r'recurring-rules', RecurringRecordingRuleViewSet, basename='recurring-rule')

urlpatterns = [
    # Bulk delete is a single APIView, not a ViewSet
    path('streams/bulk-delete/', BulkDeleteStreamsAPIView.as_view(), name='bulk_delete_streams'),
    path('channels/bulk-delete/', BulkDeleteChannelsAPIView.as_view(), name='bulk_delete_channels'),
    path('logos/bulk-delete/', BulkDeleteLogosAPIView.as_view(), name='bulk_delete_logos'),
    path('logos/cleanup/', CleanupUnusedLogosAPIView.as_view(), name='cleanup_unused_logos'),
    path('channels/<int:channel_id>/streams/', GetChannelStreamsAPIView.as_view(), name='get_channel_streams'),
    path('channels/<int:channel_id>/streams/stats/', GetChannelStreamStatsAPIView.as_view(), name='get_channel_stream_stats'),
    path('profiles/<int:profile_id>/channels/<int:channel_id>/', UpdateChannelMembershipAPIView.as_view(), name='update_channel_membership'),
    path('profiles/<int:profile_id>/channels/bulk-update/', BulkUpdateChannelMembershipAPIView.as_view(), name='bulk_update_channel_membership'),
    # DVR series rules (order matters: specific routes before catch-all slug)
    path('series-rules/', SeriesRulesAPIView.as_view(), name='series_rules'),
    path('series-rules/preview/', SeriesRulePreviewAPIView.as_view(), name='series_rules_preview'),
    path('series-rules/evaluate/', EvaluateSeriesRulesAPIView.as_view(), name='evaluate_series_rules'),
    path('series-rules/bulk-remove/', BulkRemoveSeriesRecordingsAPIView.as_view(), name='bulk_remove_series_recordings'),
    path('recordings/bulk-delete-upcoming/', BulkDeleteUpcomingRecordingsAPIView.as_view(), name='bulk_delete_upcoming_recordings'),
    path(
        'recordings/<int:pk>/hls/<path:seg_path>',
        RecordingViewSet.as_view(
            {'get': 'hls'},
            authentication_classes=RECORDING_PLAYBACK_AUTHENTICATORS,
        ),
        name='recording-hls',
    ),
    path('dvr/comskip-config/', ComskipConfigAPIView.as_view(), name='comskip_config'),
    # Logos from public collections, suggested per channel (see logo_library)
    path('logo-library/', logo_library_views.logo_library_suggestions, name='logo_library'),
    path('logo-library/status/', logo_library_views.logo_library_status, name='logo_library_status'),
    path('logo-library/refresh/', logo_library_views.logo_library_refresh, name='logo_library_refresh'),
    path('logo-library/apply/', logo_library_views.logo_library_apply, name='logo_library_apply'),
    path('logo-library/search/', logo_library_views.logo_library_search, name='logo_library_search'),
    path('logo-library/sources/', logo_library_views.logo_library_sources, name='logo_library_sources'),
    # Recognising the same channel across providers and qualities (see channel_manager)
    path('channel-manager/', channel_manager_views.channel_manager_options, name='channel_manager_options'),
    path('channel-manager/preview/', channel_manager_views.channel_manager_preview, name='channel_manager_preview'),
    path('channel-manager/apply/', channel_manager_views.channel_manager_apply, name='channel_manager_apply'),
    path('channel-manager/guides/', channel_manager_views.channel_manager_guides, name='channel_manager_guides'),
    path('channel-manager/guides/load/', channel_manager_views.channel_manager_load_guide, name='channel_manager_load_guide'),
    path('channel-manager/ignore/', channel_manager_views.channel_manager_ignore, name='channel_manager_ignore'),
    path('channel-manager/settings/', channel_manager_views.channel_manager_settings, name='channel_manager_settings'),
    path('stream-check/', stream_check_views.stream_check_overview, name='stream_check_overview'),
    path('stream-check/run/', stream_check_views.stream_check_run, name='stream_check_run'),
    path('stream-check/stop/', stream_check_views.stream_check_stop, name='stream_check_stop'),
    path('stream-check/settings/', stream_check_views.stream_check_settings, name='stream_check_settings'),
    path('stream-check/action/', stream_check_views.stream_check_action, name='stream_check_action'),
    path('stream-check/clear/', stream_check_views.stream_check_clear, name='stream_check_clear'),
    path('stream-check/limits/', stream_check_views.stream_check_limit, name='stream_check_limit'),
    # Some clients strip trailing slashes from artwork URLs. Serve the same
    # view directly (no redirect) so logo fetches still return an image.
    path(
        'logos/<int:pk>/cache',
        LogoViewSet.as_view({'get': 'cache'}),
        name='logo-cache-noslash',
    ),
]

urlpatterns += router.urls
