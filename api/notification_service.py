"""
Centralized notification service.

Call `create_notification()` from mutations, tasks, schedulers or webhooks
to create an in-app notification.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def create_notification(
    recipient,
    notification_type: str,
    title: str,
    message: str,
    *,
    recipient_role: Optional[str] = None,
    advertisement_id: Optional[int] = None,
    channel_id: Optional[int] = None,
    invoice_id: Optional[int] = None,
):
    """
    Create an in-app notification for `recipient`.

    Parameters
    ----------
    recipient : User instance
    notification_type : one of Notification.NOTIFICATION_TYPES values
    title : short headline shown in the notification bell
    message : longer body text
    recipient_role : override; defaults to recipient.role
    advertisement_id, channel_id, invoice_id : optional FK refs for deep-linking

    Returns
    -------
    Notification instance
    """
    from api.models import Notification

    role = recipient_role or getattr(recipient, "role", "user")

    notification = Notification.objects.create(
        recipient=recipient,
        notification_type=notification_type,
        title=title,
        message=message,
        recipient_role=role,
        advertisement_id=advertisement_id,
        channel_id=channel_id,
        invoice_id=invoice_id,
    )

    logger.info(
        f"Notification created: type={notification_type} → {recipient.email}"
    )

    return notification


def create_bulk_notifications(
    recipients,
    notification_type: str,
    title: str,
    message: str,
    *,
    recipient_role: Optional[str] = None,
    channel_id: Optional[int] = None,
):
    """
    Create the same notification for many recipients at once.
    Used for discount availability alerts sent to all users.
    """
    from api.models import Notification

    objs = [
        Notification(
            recipient=user,
            notification_type=notification_type,
            title=title,
            message=message,
            recipient_role=recipient_role or getattr(user, "role", "user"),
            channel_id=channel_id,
        )
        for user in recipients
    ]
    Notification.objects.bulk_create(objs, batch_size=500)
    logger.info(
        f"Bulk notification created: type={notification_type}, count={len(objs)}"
    )


PAYMENT_SETUP_COMPLETE_TITLE = "Payment Setup Complete"
PAYMENT_SETUP_COMPLETE_MESSAGE = (
    "Payment method added successfully. You now have access to all features!"
)


def notify_payment_setup_complete(user) -> bool:
    """
    Tell an approved business that its payment setup is done.

    Sent once, and only when both are true: a TWAM admin has approved the
    business and it has a card on file (checked with Stripe). It is a separate
    notification from the approval ones, so it can be triggered from either
    side: after a card is added, or when an admin approves a business that
    already has a card. Returns True when a notification was created.
    """
    from api.models import Notification
    from api.stripe_services import get_user_payment_methods

    if getattr(user, "role", None) != "business" or not user.is_admin_approved:
        return False

    if Notification.objects.filter(
        recipient=user, notification_type="payment_setup_complete"
    ).exists():
        return False

    if not (get_user_payment_methods(user) or []):
        return False

    create_notification(
        recipient=user,
        notification_type="payment_setup_complete",
        title=PAYMENT_SETUP_COMPLETE_TITLE,
        message=PAYMENT_SETUP_COMPLETE_MESSAGE,
    )
    return True
