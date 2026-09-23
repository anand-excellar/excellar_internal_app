from rest_framework import serializers
from tracker.models import Snapshot, DailyPnl, CrossMtmConfig


class SnapshotSerializer(serializers.ModelSerializer):
    """Used for list views — excludes large raw_debank_json blob."""
    class Meta:
        model = Snapshot
        exclude = ["raw_debank_json"]


class SnapshotDetailSerializer(serializers.ModelSerializer):
    """Used for detail view — includes all fields."""
    class Meta:
        model = Snapshot
        fields = "__all__"


class DailyPnlSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyPnl
        fields = "__all__"


class CrossMtmConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = CrossMtmConfig
        fields = "__all__"
