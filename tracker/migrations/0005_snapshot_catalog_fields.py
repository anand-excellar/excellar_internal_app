from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0004_remove_colend_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="wallet_native_amount",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="snapshot",
            name="perp_positions_json",
            field=models.TextField(blank=True, null=True),
        ),
    ]
