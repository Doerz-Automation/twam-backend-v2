from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower, Trim


def normalize_emails(apps, schema_editor):
    """Store every user email trimmed and lowercased (see api/email_utils.py),
    so lookups by the normalized email always find the account.

    Two accounts whose emails differ only by case or spaces (e.g. "Ann@x.com"
    and "ann@x.com") cannot both be normalized without breaking the unique
    constraint. Those are left as they are and listed in the output, so an admin
    can merge or delete the duplicate; the migration itself never deletes or
    merges accounts.
    """
    User = apps.get_model("api", "User")

    clashing = set(
        User.objects.annotate(norm=Lower(Trim("email")))
        .values("norm")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("norm", flat=True)
    )

    fixed = 0
    for user in User.objects.only("id", "email").iterator():
        norm = (user.email or "").strip().lower()
        if norm == user.email or norm in clashing:
            continue
        # update() writes just this column and skips model save() hooks
        User.objects.filter(id=user.id).update(email=norm)
        fixed += 1

    if fixed:
        print(f"\n  Normalized {fixed} user email(s).")
    if clashing:
        print(
            "\n  WARNING: these emails belong to more than one account when case "
            "and spaces are ignored. They were left unchanged; merge or delete "
            "the duplicates:\n    " + "\n    ".join(sorted(clashing))
        )


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0007_create_cache_table"),
    ]

    operations = [
        migrations.RunPython(normalize_emails, migrations.RunPython.noop),
    ]
