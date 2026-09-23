from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0008_remove_snapshot_tracker_sna_segment_ef4f74_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="hl_available_usdc",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="snapshot",
            name="binance_available_usdc",
            field=models.FloatField(blank=True, null=True),
        ),
    ]