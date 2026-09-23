import csv

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import generics, views
from rest_framework.response import Response

from tracker.config import enabled_tokens
from tracker.models import Snapshot, DailyPnl, CrossMtmConfig
from api.serializers import (
    SnapshotSerializer, SnapshotDetailSerializer,
    DailyPnlSerializer, CrossMtmConfigSerializer,
)


def _enabled_segments():
    return list(enabled_tokens().keys())


class SnapshotListView(generics.ListAPIView):
    serializer_class = SnapshotSerializer

    def get_queryset(self):
        from datetime import date as date_type
        qs = Snapshot.objects.order_by("-timestamp")
        segment = self.request.query_params.get("segment")
        date_filter = self.request.query_params.get("date")
        if segment:
            qs = qs.filter(segment=segment)
        if date_filter:
            try:
                date_type.fromisoformat(date_filter)
                qs = qs.filter(timestamp__date=date_filter)
            except ValueError:
                pass  # ignore invalid date filter, return unfiltered
        return qs


class SnapshotLatestView(views.APIView):
    def get(self, request):
        results = []
        for seg in _enabled_segments():
            snap = Snapshot.objects.filter(segment=seg).order_by("-timestamp").first()
            if snap:
                results.append(SnapshotSerializer(snap).data)
        return Response(results)


class SnapshotDetailView(generics.RetrieveAPIView):
    queryset = Snapshot.objects.all()
    serializer_class = SnapshotDetailSerializer


class DailyPnlListView(generics.ListAPIView):
    serializer_class = DailyPnlSerializer

    def get_queryset(self):
        from datetime import date as date_type
        qs = DailyPnl.objects.order_by("-date")
        segment = self.request.query_params.get("segment")
        start = self.request.query_params.get("start")
        end = self.request.query_params.get("end")
        if segment:
            qs = qs.filter(segment=segment)
        if start:
            try:
                date_type.fromisoformat(start)
                qs = qs.filter(date__gte=start)
            except ValueError:
                pass
        if end:
            try:
                date_type.fromisoformat(end)
                qs = qs.filter(date__lte=end)
            except ValueError:
                pass
        return qs


class DailyPnlDetailView(generics.RetrieveAPIView):
    serializer_class = DailyPnlSerializer

    def get_object(self):
        return get_object_or_404(
            DailyPnl,
            segment=self.kwargs["segment"],
            date=self.kwargs["date"],
        )


class CrossMtmConfigListView(generics.ListAPIView):
    queryset = CrossMtmConfig.objects.order_by("-effective_from")
    serializer_class = CrossMtmConfigSerializer


class PortfolioSummaryView(views.APIView):
    def get(self, request):
        results = []
        for seg in _enabled_segments():
            latest = DailyPnl.objects.filter(segment=seg).order_by("-date").first()
            if latest:
                results.append({
                    "segment": seg,
                    "latest_date": latest.date,
                    "portfolio_value": latest.portfolio_value,
                    "rolling_max": latest.rolling_max,
                    "drawdown": latest.drawdown,
                    "annualized_return": latest.annualized_return,
                })
        return Response(results)


class TriggerSnapshotView(views.APIView):
    def post(self, request):
        from tracker.tasks import run_snapshot_cycle
        run_snapshot_cycle()
        return Response({"status": "snapshot cycle enqueued"})


class TriggerFinalizeView(views.APIView):
    def post(self, request):
        target_date = request.data.get("date")
        if target_date:
            try:
                from datetime import date as date_type
                date_type.fromisoformat(target_date)
            except ValueError:
                return Response({"error": "Invalid date format, expected YYYY-MM-DD"}, status=400)
            from tracker.services.collector import finalize_daily_for_date
            finalize_daily_for_date(target_date)
        else:
            from tracker.tasks import run_daily_finalization
            run_daily_finalization()
        return Response({"status": "finalization triggered"})


class DailyPnlExportView(views.APIView):
    def get(self, request):
        segment = request.query_params.get("segment")
        qs = DailyPnl.objects.order_by("segment", "date")
        if segment:
            qs = qs.filter(segment=segment)

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="daily_pnl.csv"'

        if not qs.exists():
            return response

        fields = [f.name for f in DailyPnl._meta.fields]
        writer = csv.writer(response)
        writer.writerow(fields)
        for row in qs:
            writer.writerow([getattr(row, f) for f in fields])
        return response
