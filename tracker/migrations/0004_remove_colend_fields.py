from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("tracker", "0003_add_snapshot_desc_index"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="snapshot",
            name="colend_supply_usdc",
        ),
        migrations.RemoveField(
            model_name="dailypnl",
            name="colend_value",
        ),
    ]
