from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0005_snapshot_catalog_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="snapshot",
            name="bitgo_holdings_json",
            field=models.TextField(blank=True, null=True),
        ),
    ]
