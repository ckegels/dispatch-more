import logging
import os
from collections import defaultdict
from rest_framework import viewsets, status, serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView
from apps.epg.sd_api import (
    SchedulesDirectPosterMixin,
    SchedulesDirectSourceMixin,
)
from core.image_proxy import RawImageContentNegotiationMixin
from rest_framework.decorators import action
from drf_spectacular.utils import extend_schema, OpenApiParameter, inline_serializer
from drf_spectacular.types import OpenApiTypes
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import EPGSource, ProgramData, EPGData
from .serializers import (
    ProgramDataSerializer,
    ProgramDetailSerializer,
    EPGSourceSerializer,
    EPGDataSerializer,
    ProgramSearchResultSerializer,
)
from .tasks import refresh_epg_data, find_current_program_for_tvg_id
from .query_utils import parse_text_query
from apps.channels.access import channels_queryset_for_user
from apps.channels.managers import (
    effective_field_lookup_q,
    effective_related_name_lookup_q,
    with_effective_values,
)
from apps.channels.models import Channel
from apps.accounts.permissions import (
    IsAdmin,
    IsStandardUser,
    permissions_for_action,
    permissions_for_method,
)
from core.utils import safe_upload_path

logger = logging.getLogger(__name__)


# ─────────────────────────────
# 1) EPG Source API (CRUD)
# ─────────────────────────────
class EPGSourceViewSet(SchedulesDirectSourceMixin, viewsets.ModelViewSet):
    """
    API endpoint that allows EPG sources to be viewed or edited.
    """

    queryset = EPGSource.objects.select_related(
        "refresh_task__crontab", "refresh_task__interval"
    ).all()
    serializer_class = EPGSourceSerializer

    def get_permissions(self):
        if self.action in ('sd_lineups', 'sd_lineups_search'):
            if self.request.method == 'GET':
                return [IsStandardUser()]
            return [IsAdmin()]
        return permissions_for_action(self)

    def get_queryset(self):
        from django.db.models import Exists, OuterRef
        from apps.channels.models import Channel
        return EPGSource.objects.select_related(
            "refresh_task__crontab", "refresh_task__interval"
        ).annotate(
            has_channels=Exists(
                Channel.objects.filter(epg_data__epg_source_id=OuterRef('pk'))
            )
        )

    def list(self, request, *args, **kwargs):
        logger.debug("Listing all EPG sources.")
        return super().list(request, *args, **kwargs)

    @action(detail=False, methods=["post"], permission_classes=[IsAdmin])
    def upload(self, request):
        if "file" not in request.FILES:
            return Response(
                {"error": "No file uploaded"}, status=status.HTTP_400_BAD_REQUEST
            )

        file = request.FILES["file"]
        try:
            file_path = safe_upload_path(file.name, "/data/uploads/epgs")
        except ValueError:
            return Response(
                {"error": "Invalid filename"}, status=status.HTTP_400_BAD_REQUEST
            )

        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb+") as destination:
            for chunk in file.chunks():
                destination.write(chunk)

        new_obj_data = request.data.copy()
        new_obj_data["file_path"] = file_path

        serializer = self.get_serializer(data=new_obj_data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        """Handle partial updates with special logic for is_active field"""
        instance = self.get_object()

        # Check if we're toggling is_active
        if (
            "is_active" in request.data
            and instance.is_active != request.data["is_active"]
        ):
            # Set appropriate status based on new is_active value
            if request.data["is_active"]:
                request.data["status"] = "idle"
            else:
                request.data["status"] = "disabled"

        # Continue with regular partial update
        return super().partial_update(request, *args, **kwargs)


class ProgramSearchPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


def stream_list(view, request, queryset, batch_size=2000):
    """
    A list view's JSON answer written out batch by batch, or None when DRF's own path must
    answer (the browsable API, an indented answer, a view with pages).

    Built whole, a big table's answer costs far more memory than its bytes, and a uWSGI
    worker never gives that memory back (see EPGDataViewSet.list and ProgramViewSet.list).
    Each batch goes through the view's serializer and renderer, so the bytes are the ones DRF
    would have sent.
    """
    from django.http import StreamingHttpResponse
    from rest_framework.renderers import JSONRenderer

    renderer = getattr(request, "accepted_renderer", None)
    context = view.get_renderer_context()
    if (
        type(renderer) is not JSONRenderer
        or view.paginator is not None
        or renderer.get_indent(request.accepted_media_type, context) is not None
    ):
        return None
    media_type = request.accepted_media_type
    # What the renderer puts between list items (compact unless COMPACT_JSON is off)
    between = b"," if renderer.compact else b", "

    def rendered(rows):
        # "[a,b]" from the renderer, without its brackets: joined with the separator the
        # compact renderer itself puts between list items
        return renderer.render(view.get_serializer(rows, many=True).data, media_type, context)[1:-1]

    def batches():
        yield b"["
        first = True
        batch = []
        for row in queryset.iterator(chunk_size=batch_size):
            batch.append(row)
            if len(batch) >= batch_size:
                yield (b"" if first else between) + rendered(batch)
                first = False
                batch = []
        if batch:
            yield (b"" if first else between) + rendered(batch)
        yield b"]"

    return StreamingHttpResponse(batches(), content_type=renderer.media_type)


class ProgramViewSet(
    RawImageContentNegotiationMixin, SchedulesDirectPosterMixin, viewsets.ModelViewSet
):
    """Handles CRUD operations for EPG programs"""

    queryset = ProgramData.objects.select_related("epg").all()
    serializer_class = ProgramDataSerializer

    # Short process-local cooldown for transient poster errors (auth/network).
    # Image download limits are persisted on the EPG source (shared across workers).

    def get_permissions(self):
        return permissions_for_action(self)

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProgramDetailSerializer
        return ProgramDataSerializer

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    # The most a page holds, and rows per batch of the streamed list (see list())
    MAX_PAGE_SIZE = 1000
    STREAM_BATCH = 2000

    def list(self, request, *args, **kwargs):
        """
        Every programme, streamed -- or one page of them, when a page is asked for.

        Stock answers this with every programme of every guide built into one response: no
        pages, `page_size` and `tvg_id` ignored. With days of guide history that is millions
        of rows. On 2026-11-09 one request from arrTV, falling back to
        `?page_size=1000` when its grid request failed, took a web worker from 207 MB to
        7.9 GB in 50 seconds and the server with it, as `/api/epg/epgdata/` did on
        2026-10-20. So:

        - `page_size` or `page` asked for: one page, at most MAX_PAGE_SIZE rows, ordered by
          start time, in DRF's page shape (`next`, `previous`, `results`; `count` is null, as
          counting millions of rows for every page costs more than the page). Only programmes
          that have not ended: the one caller, arrTV's `getBulkUpcomingPrograms` (its guide
          when the grid fails, beside `current-programs`), wants what is on next and reads ten
          pages at most -- from the oldest, those would be ten thousand programmes of the
          guide's history and nothing to come;
        - `tvg_id` asked for: only that guide's programmes, past ones included (arrTV's
          `getProgramsByTvgId` looks for a recording that has ended);
        - neither: the same rows stock sends, byte for byte, in id order, written out batch by
          batch (stream_list).
        """
        logger.debug("Listing all EPG programs.")
        queryset = self.filter_queryset(self.get_queryset())
        tvg_id = request.query_params.get("tvg_id")
        if tvg_id:
            queryset = queryset.filter(tvg_id=tvg_id)
        if "page_size" in request.query_params or "page" in request.query_params:
            if not tvg_id:
                queryset = queryset.filter(end_time__gt=timezone.now())
            return self._page(request, queryset)
        # Stock's list has no order: Postgres hands rows over as its plan finds them, and the
        # streamed read (a server-side cursor) may plan differently from a whole read. In id
        # order -- the order rows were written, which is what stock gives in practice -- the
        # answer is the same every time, through the primary key's index.
        streamed = stream_list(self, request, queryset.order_by("id"), self.STREAM_BATCH)
        return streamed if streamed is not None else super().list(request, *args, **kwargs)

    def _page(self, request, queryset):
        from rest_framework.utils.urls import remove_query_param, replace_query_param

        def number(name, default):
            try:
                return max(1, int(request.query_params.get(name) or default))
            except (TypeError, ValueError):
                return default

        size = min(number("page_size", self.MAX_PAGE_SIZE), self.MAX_PAGE_SIZE)
        page = number("page", 1)
        start = (page - 1) * size
        # One row more than the page, to know whether there is a next one without counting
        rows = list(queryset.order_by("start_time", "id")[start:start + size + 1])
        url = request.build_absolute_uri()
        previous = None
        if page > 1:
            previous = (remove_query_param(url, "page") if page == 2
                        else replace_query_param(url, "page", page - 1))
        return Response({
            "count": None,
            "next": replace_query_param(url, "page", page + 1) if len(rows) > size else None,
            "previous": previous,
            "results": self.get_serializer(rows[:size], many=True).data,
        })

    @extend_schema(
        summary="Search EPG programs",
        description="""
**Advanced EPG program search with multiple filter types and complex query support.**

### Text Search Features

**Title and Description Search**:
- Supports AND/OR logical operators (case-insensitive: `and`/`AND` both work)
- Wrap phrases in double quotes to match them literally: `"Law and Order"`
- Parenthetical grouping for complex queries: `(Newcastle OR NEW) AND (Villa OR AST)`
- Regex pattern matching with `title_regex=true` (evaluated by the database engine)
- Whole word matching with `title_whole_words=true` to avoid partial matches

**Examples**:
- Simple: `title=football`
- AND operator: `title=premier AND league`
- OR operator: `title=Newcastle OR Villa`
- Quoted phrase: `title="Law and Order"` (matches the exact phrase; 'and' is literal)
- Mixed: `title="Law and Order" AND crime`
- Nested groups: `title=(Newcastle OR NEW) AND (Villa OR AST)`
- Regex: `title=^Premier&title_regex=true` (programs starting with "Premier")
- Whole words: `title=NEW&title_whole_words=true` (matches "NEW" but not "News")

### Time Filtering

**airing_at**: Find programs airing at a specific moment (start_time ≤ airing_at < end_time)

**Time ranges**: Use combinations of start_after, start_before, end_after, end_before

### Response Customization

**fields**: Comma-separated list to include only specific fields in response
- Available: id, title, sub_title, description, start_time, end_time, tvg_id, custom_properties, epg_source, epg_name, epg_icon_url, channels, streams

### Pagination

- Default: 50 results per page
- Maximum: 500 results per page
- Use `page` and `page_size` parameters to navigate results
        """,
        parameters=[
            OpenApiParameter(
                'title',
                OpenApiTypes.STR,
                description='Title search query. Supports AND/OR operators (case-insensitive), quoted phrases, and parentheses. Double-quote a phrase to match it literally: `"Law and Order"`. Unquoted space-separated terms are matched as a phrase; use AND/OR to combine separate terms.',
            ),
            OpenApiParameter('title_regex', OpenApiTypes.BOOL, description='Enable regex matching for title (case-insensitive, default: false). e.g. `^The` matches titles starting with "The".'),
            OpenApiParameter('title_whole_words', OpenApiTypes.BOOL, description='Match whole words only in title (default: false). e.g. `new` matches "Newcastle" normally but not with whole words enabled.'),
            OpenApiParameter(
                'description',
                OpenApiTypes.STR,
                description='Description search query. Same syntax and features as title search.'
            ),
            OpenApiParameter('description_regex', OpenApiTypes.BOOL, description='Enable regex matching for description (case-insensitive, default: false).'),
            OpenApiParameter('description_whole_words', OpenApiTypes.BOOL, description='Match whole words only in description (default: false). Same behaviour as title_whole_words.'),
            OpenApiParameter('start_after', OpenApiTypes.DATETIME, description='Filter programs starting at or after this time. ISO 8601 format, e.g. `2026-02-14T18:00:00Z`.'),
            OpenApiParameter('start_before', OpenApiTypes.DATETIME, description='Filter programs starting at or before this time. ISO 8601 format.'),
            OpenApiParameter('end_after', OpenApiTypes.DATETIME, description='Filter programs ending at or after this time. ISO 8601 format.'),
            OpenApiParameter('end_before', OpenApiTypes.DATETIME, description='Filter programs ending at or before this time. ISO 8601 format.'),
            OpenApiParameter('airing_at', OpenApiTypes.DATETIME, description='Find programs airing at this exact moment (start_time ≤ airing_at < end_time). ISO 8601 format, e.g. `2026-02-14T20:00:00Z`.'),
            OpenApiParameter('channel', OpenApiTypes.STR, description='Filter by channel name (case-insensitive substring match). e.g. `BBC One`, `Sky Sports`.'),
            OpenApiParameter('channel_id', OpenApiTypes.INT, description='Filter by exact channel ID.'),
            OpenApiParameter('tvg_id', OpenApiTypes.STR, description='Filter by EPG tvg_id (exact match). e.g. `bbcone.uk`.'),
            OpenApiParameter('stream', OpenApiTypes.STR, description='Filter by stream name (case-insensitive substring match).'),
            OpenApiParameter('group', OpenApiTypes.STR, description='Filter by channel group or stream group name (case-insensitive substring match). e.g. `Sports`, `UK Channels`.'),
            OpenApiParameter('epg_source', OpenApiTypes.INT, description='Filter by EPG source ID.'),
            OpenApiParameter('fields', OpenApiTypes.STR, description='Comma-separated list of fields to include. Omit to return all fields. e.g. `title,start_time,end_time`.'),
            OpenApiParameter('page', OpenApiTypes.INT, description='Page number for pagination (default: 1).'),
            OpenApiParameter('page_size', OpenApiTypes.INT, description='Results per page (default: 50, max: 500).'),
        ],
        responses={200: ProgramSearchResultSerializer(many=True)},
        tags=['EPG'],
    )
    @action(detail=False, methods=['get'], url_path='search', permission_classes=[IsStandardUser])
    def search(self, request):
        params = request.query_params

        # Channel rows are loaded once for the page below. Prefetching
        # epg.channels here would miss override-only links and duplicate that work.
        queryset = ProgramData.objects.select_related(
            'epg', 'epg__epg_source'
        )

        filters = Q()

        # Text filters
        title = params.get('title')
        if title:
            title_regex = params.get('title_regex', '').lower() in ('true', '1', 'yes')
            title_whole_words = params.get('title_whole_words', '').lower() in ('true', '1', 'yes')
            filters &= parse_text_query('title', title, use_regex=title_regex, whole_words=title_whole_words)

        description = params.get('description')
        if description:
            desc_regex = params.get('description_regex', '').lower() in ('true', '1', 'yes')
            desc_whole_words = params.get('description_whole_words', '').lower() in ('true', '1', 'yes')
            filters &= parse_text_query('description', description, use_regex=desc_regex, whole_words=desc_whole_words)

        # Time filters with validation
        start_after = params.get('start_after')
        if start_after:
            dt = parse_datetime(start_after)
            if dt is None:
                return Response(
                    {"error": f"Invalid datetime format for start_after: {start_after}. Use ISO 8601 format (e.g., 2026-02-14T18:00:00Z)"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            filters &= Q(start_time__gte=dt)

        start_before = params.get('start_before')
        if start_before:
            dt = parse_datetime(start_before)
            if dt is None:
                return Response(
                    {"error": f"Invalid datetime format for start_before: {start_before}. Use ISO 8601 format."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            filters &= Q(start_time__lte=dt)

        end_after = params.get('end_after')
        if end_after:
            dt = parse_datetime(end_after)
            if dt is None:
                return Response(
                    {"error": f"Invalid datetime format for end_after: {end_after}. Use ISO 8601 format."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            filters &= Q(end_time__gte=dt)

        end_before = params.get('end_before')
        if end_before:
            dt = parse_datetime(end_before)
            if dt is None:
                return Response(
                    {"error": f"Invalid datetime format for end_before: {end_before}. Use ISO 8601 format."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            filters &= Q(end_time__lte=dt)

        airing_at = params.get('airing_at')
        if airing_at:
            dt = parse_datetime(airing_at)
            if dt is None:
                return Response(
                    {"error": f"Invalid datetime format for airing_at: {airing_at}. Use ISO 8601 format."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            filters &= Q(start_time__lte=dt, end_time__gt=dt)

        def effective_epg_ids(qs):
            # One Coalesce for the filter. with_effective_values() annotates
            # every overridable column and is only worth it when serializing.
            return (
                qs.annotate(
                    _eff_epg_id=Coalesce("override__epg_data_id", "epg_data_id")
                )
                .exclude(_eff_epg_id__isnull=True)
                .values("_eff_epg_id")
                .distinct()
            )

        # Channel/stream filters resolve against effective channel mappings
        # (Channel.epg_data or ChannelOverride.epg_data) so override-only
        # links still match.
        channel = params.get('channel')
        if channel:
            filters &= Q(epg_id__in=effective_epg_ids(
                Channel.objects.filter(
                    effective_field_lookup_q("name", "icontains", channel)
                )
            ))

        channel_id = params.get('channel_id')
        if channel_id:
            try:
                filters &= Q(epg_id__in=effective_epg_ids(
                    Channel.objects.filter(id=int(channel_id))
                ))
            except (ValueError, TypeError):
                pass

        tvg_id = params.get('tvg_id')
        if tvg_id:
            filters &= Q(epg__tvg_id=tvg_id)

        stream = params.get('stream')
        if stream:
            filters &= Q(epg_id__in=effective_epg_ids(
                Channel.objects.filter(streams__name__icontains=stream)
            ))

        group = params.get('group')
        if group:
            filters &= Q(epg_id__in=effective_epg_ids(
                Channel.objects.filter(
                    effective_related_name_lookup_q("channel_group", group)
                    | Q(streams__channel_group__name__icontains=group)
                )
            ))

        epg_source = params.get('epg_source')
        if epg_source:
            try:
                filters &= Q(epg__epg_source__id=int(epg_source))
            except (ValueError, TypeError):
                pass

        # Channel filters are epg_id subqueries, so each program row stays unique.
        queryset = queryset.filter(filters).order_by('start_time')

        # Restrict results to programs on channels the user can access,
        # including override-only EPG mappings.
        user = request.user
        if user.user_level < 10:
            access_qs = channels_queryset_for_user(Channel.objects.all(), user)
            accessible_epg_ids = effective_epg_ids(access_qs)
            queryset = queryset.filter(epg_id__in=accessible_epg_ids)

        # Resolve field selection before serialization so expensive methods can short-circuit
        requested_fields = params.get('fields')
        allowed = set(f.strip() for f in requested_fields.split(',')) if requested_fields else None

        # Paginate
        paginator = ProgramSearchPagination()
        page = paginator.paginate_queryset(queryset, request)

        # Bulk-load effective channel mappings for the page (one query,
        # covers override-only links, avoids N+1 in the serializer).
        channels_by_epg_id = {}
        needs_channels = allowed is None or "channels" in allowed or "streams" in allowed
        if needs_channels and page:
            epg_ids = {prog.epg_id for prog in page if prog.epg_id}
            channels_by_epg_id = defaultdict(list)
            if epg_ids:
                # Indexable FK predicates for the effective EPG id: an
                # override pin, or the channel FK when no pin is set.
                mapped = with_effective_values(
                    Channel.objects.filter(
                        Q(override__epg_data_id__in=epg_ids)
                        | (
                            Q(override__epg_data_id__isnull=True)
                            & Q(epg_data_id__in=epg_ids)
                        )
                    )
                ).select_related("channel_group", "override__channel_group")
                if user.user_level < 10:
                    mapped = channels_queryset_for_user(mapped, user)
                if allowed is None or "streams" in allowed:
                    mapped = mapped.prefetch_related(
                        "streams",
                        "streams__channel_group",
                        "streams__m3u_account",
                    )
                for ch in mapped:
                    channels_by_epg_id[ch.effective_epg_data_id].append(ch)

        serializer = ProgramSearchResultSerializer(
            page,
            many=True,
            context={
                "fields": allowed,
                "user": request.user,
                "channels_by_epg_id": channels_by_epg_id,
            },
        )
        data = serializer.data

        if allowed:
            data = [{k: v for k, v in item.items() if k in allowed} for item in data]

        return paginator.get_paginated_response(data)


# ─────────────────────────────
# 3) EPG Import View
# ─────────────────────────────
class EPGImportAPIView(APIView):
    """Triggers an EPG data refresh"""

    def get_permissions(self):
        return permissions_for_method(self.request)

    @extend_schema(
        description="Triggers an EPG data refresh for the given source.",
        request=inline_serializer(
            name="EPGImportRequest",
            fields={
                "id": serializers.IntegerField(help_text="ID of the EPG source to refresh."),
            },
        ),
    )
    def post(self, request, format=None):
        logger.info("EPGImportAPIView: Received request to import EPG data.")
        epg_id = request.data.get("id", None)
        force = bool(request.data.get("force", False))

        # Reject dummy sources with a narrow existence query, no full row load.
        if epg_id is not None:
            from .models import EPGSource

            if EPGSource.objects.filter(
                id=epg_id, source_type="dummy"
            ).exists():
                logger.info(
                    "EPGImportAPIView: Skipping refresh for dummy EPG source %s",
                    epg_id,
                )
                return Response(
                    {
                        "success": False,
                        "message": "Dummy EPG sources do not require refreshing.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        refresh_epg_data.delay(epg_id, force=force)  # Trigger Celery task
        logger.info("EPGImportAPIView: Task dispatched to refresh EPG data.")
        return Response(
            {"success": True, "message": "EPG data refresh initiated."},
            status=status.HTTP_202_ACCEPTED,
        )


# ─────────────────────────────
# 4) EPG Data View
# ─────────────────────────────
class EPGDataViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint that allows EPGData objects to be viewed.
    """

    queryset = EPGData.objects.all()
    serializer_class = EPGDataSerializer

    def get_permissions(self):
        return permissions_for_action(self)

    # Rows per serialized batch of the streamed list (see list()).
    STREAM_BATCH = 2000

    def list(self, request, *args, **kwargs):
        """
        The whole table, written out batch by batch instead of built whole first.

        This list has no pages, so every call used to serialize every guide record of every
        source in one go: about 130 MB of Python objects for a 15 MB answer on the user's
        server (2026-10-20, measured per request on the live box), memory a uWSGI worker never
        gives back. Requests piling up in one worker (it takes up to 400 at once, and keeps
        building after the client gave up) took a worker to 6 GB and the container down with
        it. Each batch goes through the same serializer and the same renderer, so the bytes
        are the ones DRF would have sent; anything else (the browsable API, an indented or
        paginated answer) keeps DRF's own path.
        """
        streamed = stream_list(self, request, self.filter_queryset(self.get_queryset()),
                               self.STREAM_BATCH)
        return streamed if streamed is not None else super().list(request, *args, **kwargs)


# ─────────────────────────────
# 5) Current Programs API
# ─────────────────────────────
class CurrentProgramsAPIView(APIView):
    """
    Lightweight endpoint that returns currently playing programs for specified channel IDs.
    Accepts POST with JSON body containing channel_ids array, or null/empty to fetch all channels.
    """

    def get_permissions(self):
        return permissions_for_method(self.request)

    @extend_schema(
        description="Get currently playing programs for specified channels or all channels",
        request=inline_serializer(
            name="CurrentProgramsRequest",
            fields={
                "channel_uuids": serializers.ListField(
                    child=serializers.CharField(),
                    required=False,
                    allow_null=True,
                    help_text="Array of channel UUIDs. If null or omitted, returns all channels with current programs.",
                ),
                "epg_data_ids": serializers.ListField(
                    child=serializers.IntegerField(),
                    required=False,
                    allow_null=True,
                    help_text="Array of EPG data IDs. Can be used instead of channel_ids.",
                ),
            },
        ),
        responses={200: ProgramDataSerializer(many=True)},
    )
    def post(self, request, format=None):
        # Get IDs from request body
        channel_uuids = request.data.get('channel_uuids', None)
        epg_data_ids = request.data.get('epg_data_ids', None)

        # Validate that at most one type of ID is provided
        if channel_uuids is not None and epg_data_ids is not None:
            return Response(
                {"error": "Provide either channel_uuids or epg_data_ids, not both"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Get current time
        now = timezone.now()

        # If epg_data_ids are provided, query directly by EPG data
        if epg_data_ids is not None:
            if not isinstance(epg_data_ids, list):
                return Response(
                    {"error": "epg_data_ids must be an array of integers or null"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            try:
                epg_data_ids = [int(eid) for eid in epg_data_ids]
            except (ValueError, TypeError):
                return Response(
                    {"error": "epg_data_ids must contain valid integers"},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Limit to 50 IDs per request
            epg_data_ids = epg_data_ids[:50]

            epg_data_entries = EPGData.objects.select_related('epg_source').filter(
                id__in=epg_data_ids
            )

            # Batch-fetch current programs for all requested EPG entries in one query
            db_programs = ProgramData.objects.filter(
                epg__in=epg_data_entries, start_time__lte=now, end_time__gt=now
            ).select_related('epg')
            # Map epg_data id -> first matching program
            programs_by_epg = {}
            for prog in db_programs:
                if prog.epg_id not in programs_by_epg:
                    programs_by_epg[prog.epg_id] = prog

            current_programs = []
            for epg_data in epg_data_entries:
                # Check batch-fetched DB results first
                program = programs_by_epg.get(epg_data.id)

                if program:
                    program_data = ProgramDataSerializer(program).data
                    program_data['epg_data_id'] = epg_data.id
                    current_programs.append(program_data)
                    continue

                # Skip dummy sources
                if epg_data.epg_source and epg_data.epg_source.source_type == 'dummy':
                    continue

                # Fall back to byte-offset index lookup, pass the object to avoid re-fetch
                result = find_current_program_for_tvg_id(epg_data)

                if result == "timeout":
                    current_programs.append({
                        "epg_data_id": epg_data.id,
                        "parsing": True,
                    })
                elif result is not None:
                    result['epg_data_id'] = epg_data.id
                    current_programs.append(result)

            return Response(current_programs, status=status.HTTP_200_OK)

        # Otherwise, use channel-based query. Honour ChannelOverride.epg_data
        # via Coalesce; filtering on Channel.epg_data alone skips override-only
        # assignments (editor Current Program uses epg_data_ids and is fine).
        from django.db.models.functions import Coalesce
        from apps.channels.models import Channel

        query = Channel.objects.annotate(
            effective_epg_data_id=Coalesce("override__epg_data_id", "epg_data_id"),
        ).exclude(effective_epg_data_id__isnull=True)

        if channel_uuids is not None:
            if not isinstance(channel_uuids, list):
                return Response(
                    {"error": "channel_uuids must be an array of strings or null"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            query = query.filter(uuid__in=channel_uuids)

        current_programs = []

        for channel in query:
            program = ProgramData.objects.select_related("epg").filter(
                epg_id=channel.effective_epg_data_id,
                start_time__lte=now,
                end_time__gt=now
            ).first()

            if program:
                program_data = ProgramDataSerializer(program).data
                program_data['channel_uuid'] = str(channel.uuid)
                current_programs.append(program_data)


        return Response(current_programs, status=status.HTTP_200_OK)
