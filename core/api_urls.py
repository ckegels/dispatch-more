# core/api_urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_views import (
    UserAgentViewSet,
    StreamProfileViewSet,
    OutputProfileViewSet,
    CoreSettingsViewSet,
    SystemNotificationViewSet,
    environment,
    version,
    capabilities,
    app_reports,
    modified_build,
    log_center_sources,
    log_center_read,
    log_center_download,
    log_center_bundle,
    modified_build_uninstall,
    rehash_streams_endpoint,
    TimezoneListView,
    get_system_events
)
from .log_files import (
    get_log_file,
    list_log_files,
    download_log_file,
)

router = DefaultRouter()
router.register(r'useragents', UserAgentViewSet, basename='useragent')
router.register(r'streamprofiles', StreamProfileViewSet, basename='streamprofile')
router.register(r'outputprofiles', OutputProfileViewSet, basename='outputprofile')
router.register(r'settings', CoreSettingsViewSet, basename='coresettings')
router.register(r'notifications', SystemNotificationViewSet, basename='systemnotification')
urlpatterns = [
    path('settings/env/', environment, name='token_refresh'),
    path('version/', version, name='version'),
    path('capabilities/', capabilities, name='capabilities'),
    path('app-reports/', app_reports, name='app_reports'),
    path('modified-build/', modified_build, name='modified_build'),
    path('log-center/', log_center_sources, name='log_center_sources'),
    path('log-center/read/', log_center_read, name='log_center_read'),
    path('log-center/download/', log_center_download, name='log_center_download'),
    path('log-center/bundle/', log_center_bundle, name='log_center_bundle'),
    path('modified-build/uninstall/', modified_build_uninstall, name='modified_build_uninstall'),
    path('rehash-streams/', rehash_streams_endpoint, name='rehash_streams'),
    path('timezones/', TimezoneListView.as_view(), name='timezones'),
    path('system-events/', get_system_events, name='system_events'),
    path('logs/', list_log_files, name='log_files'),
    path('logs/<str:name>/download/', download_log_file, name='log_file_download'),
    path('logs/<str:name>/', get_log_file, name='log_file'),
    path('', include(router.urls)),
]
