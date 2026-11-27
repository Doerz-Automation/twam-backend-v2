"""
The one place that decides what a user's email looks like.

Emails are stored and compared trimmed and lowercased, so "Ann@Example.com ",
"ann@example.com" and " ANN@EXAMPLE.COM" are the same account in every flow:
sign-up, OTP, login, forgot password, email change, admin, rate limits.

Do not lowercase emails by hand anywhere else. Store through the User model (its
save() normalizes) and look users up with User.objects.get_by_email() /
email_exists(), which use this function.
"""


def normalize_email(value):
    """Trimmed, lowercased email, or "" for an empty value."""
    return (value or "").strip().lower()
