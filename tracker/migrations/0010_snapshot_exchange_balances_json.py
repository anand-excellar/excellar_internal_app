from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0009_snapshot_hl_available_usdc"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="exchange_balances_json",
            field=models.TextField(blank=True, null=True),
        ),
    ]