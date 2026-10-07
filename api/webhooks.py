import json
import logging
import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone
from api.models import MonthlyInvoice

logger = logging.getLogger(__name__)

@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get('HTTP_STRIPE_SIGNATURE')
    endpoint_secret = settings.STRIPE_WEBHOOK_SECRET

    event = None

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError as e:
        # Invalid payload
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        return HttpResponse(status=400)

    # Handle the event
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        handle_payment_succeeded(payment_intent)
    elif event['type'] == 'payment_intent.payment_failed':
        payment_intent = event['data']['object']
        handle_payment_failed(payment_intent)
    # ... handle other event types if needed

    return HttpResponse(status=200)

def handle_payment_succeeded(payment_intent):
    invoice_id = payment_intent.get('metadata', {}).get('invoice_id')
    if not invoice_id:
        logger.warning(f"PaymentIntent {payment_intent['id']} succeeded but has no invoice_id metadata.")
        return

    try:
        from django.db import transaction
        from api.models import Advertisement, Listings
        
        invoice = MonthlyInvoice.objects.get(id=invoice_id)
        previous_status = invoice.status
        
        # Only update if not already paid
        if invoice.status != 'paid':
            with transaction.atomic():
                invoice.status = 'paid'
                invoice.stripe_payment_intent_id = payment_intent['id']
                invoice.grace_expires_at = None  # Clear grace period
                invoice.save()

                # Mark ads as paid
                invoice.advertisements.update(
                    billing_status='paid',
                    last_billed_at=timezone.now()
                )
                
                # If recovering from grace period, restore services
                if previous_status == 'grace':
                    user = invoice.user
                    
                    # Restore advertisements
                    ads_restored = Advertisement.objects.filter(
                        user=user,
                        advertisement_status='flagged'
                    ).update(advertisement_status='active')
                    
                    # Restore listings
                    listings_restored = Listings.objects.filter(
                        user=user,
                        listing_status='flagged'
                    ).update(listing_status='active')
                    
                    # Restore business profile
                    business_restored = False
                    if hasattr(user, 'business_profile'):
                        business_profile = user.business_profile
                        business_profile.is_suspended = False
                        business_profile.save(update_fields=['is_suspended'])
                        business_restored = True
                    
                    logger.info(
                        f"Invoice {invoice_id} recovered from grace period. "
                        f"Services restored for {user.email}: "
                        f"Ads={ads_restored}, Listings={listings_restored}, Business={business_restored}"
                    )

                    # Notify: payment recovered from grace
                    from api.notification_service import create_notification
                    create_notification(
                        recipient=user,
                        notification_type="payment_success",
                        title="Payment received – services restored",
                        message=f"Your payment of ${invoice.total_amount} was successful. Your ads, listings, and business profile have been restored.",
                        invoice_id=invoice.id,
                    )
                else:
                    logger.info(f"Invoice {invoice_id} marked as PAID via webhook.")

                    # Notify: normal payment success
                    from api.notification_service import create_notification
                    create_notification(
                        recipient=invoice.user,
                        notification_type="payment_success",
                        title="Payment successful",
                        message=f"Your payment of ${invoice.total_amount} has been processed successfully.",
                        invoice_id=invoice.id,
                    )
                    
    except MonthlyInvoice.DoesNotExist:
        logger.error(f"Invoice {invoice_id} not found for PaymentIntent {payment_intent['id']}.")


def handle_payment_failed(payment_intent):
    invoice_id = payment_intent.get('metadata', {}).get('invoice_id')
    if not invoice_id:
        # Could be a payment not related to our invoice system
        return 

    try:
        invoice = MonthlyInvoice.objects.get(id=invoice_id)
        
        # Start grace period (3 days)
        invoice.start_grace_period(days=3)
        
        logger.warning(
            f"Invoice {invoice_id} payment failed. Grace period started. "
            f"Expires at: {invoice.grace_expires_at}"
        )
        
        # Notify business about payment failure
        from api.notification_service import create_notification
        create_notification(
            recipient=invoice.user,
            notification_type="payment_failed",
            title="Payment failed – action required",
            message=f"Your payment of ${invoice.total_amount} has failed. A 3-day grace period has started. Please update your payment method to avoid service disruption.",
            invoice_id=invoice.id,
        )
        
    except MonthlyInvoice.DoesNotExist:
        logger.error(f"Invoice {invoice_id} not found for failed PaymentIntent {payment_intent['id']}.")




