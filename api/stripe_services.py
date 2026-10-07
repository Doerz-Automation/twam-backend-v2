import stripe
from django.conf import settings
from .models import User
import logging

logger = logging.getLogger(__name__)


stripe.api_key = settings.STRIPE_SECRET_KEY

def create_stripe_customer(user: User) -> str:
    """
    Get or create a Stripe Customer for the given user.
    """
    if not stripe.api_key:
        logger.warning("Stripe API key not set.")
        return "test_customer_id"

    # Search for existing customer by email
    customers = stripe.Customer.list(email=user.email, limit=1)
    if customers and customers.data:
        customer_id = customers.data[0].id
        if user.stripe_customer_id != customer_id:
            user.stripe_customer_id = customer_id
            user.save()
        return customer_id

    # Create new customer
    customer = stripe.Customer.create(
        email=user.email,
        name=f"{user.first_name} {user.last_name}",
        metadata={
            "user_id": str(user.id)
        }
    )
    user.stripe_customer_id = customer.id
    user.save()
    return customer.id

def get_user_payment_methods(user: User):
    """
    List payment methods for the user.
    """
    create_stripe_customer(user) # Ensure customer exists
    
    if not user.stripe_customer_id:
        return []

    try:
        # Fetch customer to get default payment method
        customer = stripe.Customer.retrieve(user.stripe_customer_id)
        default_payment_method_id = customer.invoice_settings.default_payment_method

        payment_methods = stripe.PaymentMethod.list(
            customer=user.stripe_customer_id,
            type="card",
        )
        
        # Sort: Default first, then others
        pms = payment_methods.data
        if default_payment_method_id:
            pms.sort(key=lambda x: x.id == default_payment_method_id, reverse=True)
            
        return pms
    except Exception as e:
        logger.error(f"Error fetching payment methods: {e}")
        return []

def set_customer_default_payment_method(user: User, payment_method_id: str):
    """
    Set the default payment method for the customer.
    """
    if not stripe.api_key or not user.stripe_customer_id:
        return False
        
    try:
        stripe.Customer.modify(
            user.stripe_customer_id,
            invoice_settings={
                "default_payment_method": payment_method_id
            }
        )
        return True
    except stripe.error.StripeError as e:
        logger.error(f"Error setting default payment method: {e}")
        raise Exception(f"Failed to update default payment method: {str(e)}")



def cancel_subscription(subscription_id: str):
    """
    Cancel subscription immediately with proration and refund the unused amount to the card.
    """
    if not stripe.api_key:
        return

    try:
        # 1. Retrieve subscription to get customer and latest invoice details
        sub = stripe.Subscription.retrieve(subscription_id)
        customer_id = sub.customer
        latest_invoice_id = sub.latest_invoice

        # 2. Delete subscription with proration (creates a credit on customer balance)
        stripe.Subscription.delete(
            subscription_id,
            prorate=True
        )

        # 3. Retrieve customer to check the credit balance created by proration
        customer = stripe.Customer.retrieve(customer_id)
        
        logger.info(f"DEBUG: Customer {customer_id} balance after cancel: {customer.balance}")
        
        # Check if there is a credit balance (negative value)
        if customer.balance < 0:
             # 4. Retrieve the latest invoice to get the payment intent/charge to refund
            if latest_invoice_id:
                invoice = stripe.Invoice.retrieve(latest_invoice_id)
                logger.info(f"DEBUG: Latest invoice retrieved for refund: {latest_invoice_id}")
                payment_intent_id = invoice.payment_intent

                if payment_intent_id:
                     # 5. Issue the refund
                    refund_amount = abs(customer.balance)
                    logger.info(f"DEBUG: Initiating refund of {refund_amount} for PI {payment_intent_id}")
                    stripe.Refund.create(
                        payment_intent=payment_intent_id,
                        amount=refund_amount
                    )
                    
                    # 6. Reset customer balance to 0 (since we refunded it)
                    stripe.Customer.modify(
                        customer_id,
                        balance=0
                    )
                    logger.info(f"Refunded {refund_amount} to payment_intent {payment_intent_id}")
                else:
                    logger.warning("DEBUG: Invoice has no payment_intent, cannot refund to card.")
            else:
                logger.warning("DEBUG: Subscription has no latest_invoice, cannot trace payment for refund.")
        else:
            logger.info("DEBUG: Customer balance is not negative. No refund needed or proration resulted in 0 credit.")

    except stripe.error.InvalidRequestError as e:
        # If subscription is already deleted or doesn't exist, we consider it cancelled.
        # Check HTTP status 404 or specific code 'resource_missing'
        if e.http_status == 404 or e.code == 'resource_missing' or "No such subscription" in str(e):
             logger.warning(f"Subscription {subscription_id} not found in Stripe. Assuming already deleted.")
             return
        else:
             logger.error(f"Stripe Invalid Request: {str(e)}")
             raise Exception(f"Cancellation failed: {str(e)}")

    except stripe.error.StripeError as e:
        logger.error(f"Stripe Cancel/Refund Error: {str(e)}")
        # We might want to allow status change even if refund fails, but for now raise to alert user.
        raise Exception(f"Cancellation failed: {str(e)}")

def create_setup_intent(user: User) -> str:
    """
    Create a SetupIntent to collect a new payment method.
    """
    customer_id = create_stripe_customer(user)
    
    try:
        intent = stripe.SetupIntent.create(
            customer=customer_id,
            usage="off_session", # We intend to charge this card later (subscriptions)
        )
        return intent.client_secret
    except stripe.error.StripeError as e:
        logger.error(f"Stripe SetupIntent Error: {str(e)}")
        raise Exception(f"Failed to initialize card setup: {str(e)}")

def charge_customer_off_session(customer_id: str, amount_cents: int, payment_method_id: str, description: str = "Monthly Ad Invoice", metadata: dict = None):
    """
    Charge a customer's saved payment method off-session.
    """
    if not stripe.api_key:
        logger.warning("Stripe API key not set")
        # return a mock success for testing if no key
        return "pi_mock_123456789"

    try:
        payment_intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency="usd",
            customer=customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            description=description,
            metadata=metadata or {}
        )
        return payment_intent.id

    except stripe.error.CardError as e:
        err = e.error
        # Error code will be authentication_required if authentication is needed
        logger.error(f"Card Error: {err.code}")
        payment_intent_id = err.payment_intent['id']
        payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        raise Exception(f"Payment failed: {err.message}")

    except stripe.error.StripeError as e:
        logger.error(f"Stripe Error: {str(e)}")
        raise Exception(f"Payment failed: {str(e)}")

