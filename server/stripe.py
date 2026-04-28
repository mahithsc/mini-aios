from __future__ import annotations

import os

import stripe
from dotenv import load_dotenv

load_dotenv()

publishable_key = os.environ["STRIPE_PUBLISHABLE_KEY"]
secret_key = os.environ["STRIPE_SECRET_KEY"]
price_id = os.environ["STRIPE_PRICE_ID"]
product_id = os.getenv("STRIPE_PRODUCT_ID")

stripe.api_key = secret_key

client = stripe


def get_stripe_client():
    return client
