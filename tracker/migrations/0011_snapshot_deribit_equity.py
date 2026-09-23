from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0010_snapshot_exchange_balances_json"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="deribit_equity_usd",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="snapshot",
            name="deribit_available_usd",
            field=models.FloatField(blank=True, null=True),
        ),
    ]