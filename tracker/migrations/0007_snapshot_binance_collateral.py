from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0006_snapshot_bitgo_holdings"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="binance_collateral_usdc",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
