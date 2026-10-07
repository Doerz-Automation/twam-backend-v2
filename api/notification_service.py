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
