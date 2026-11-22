from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # Table for the database cache (settings.CACHES) that holds the rate-limit
    # counters. createcachetable skips tables that already exist.
    call_command("createcachetable", verbosity=0)


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0006_listings_reused_from"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
