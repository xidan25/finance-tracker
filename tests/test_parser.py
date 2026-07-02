"""Tests for parser.parse_scotiabank_body."""

from finance_tracker.parser import parse_scotiabank_body


def test_authorization_email_basic():
    body = (
        "Hi ALEX, There was an authorization without the credit card present "
        "for $5.65 at FREEDOM MOBILE on account 4111*****111**** at 9:53 am ET. "
        "If you didn't do this, please call 1-800-472-6842."
    )
    p = parse_scotiabank_body(body, subject="Authorization without credit card present")
    assert p is not None
    assert p.amount == 5.65
    assert p.currency == "CAD"
    assert p.merchant_raw == "FREEDOM MOBILE"
    assert p.status == "pending"
    assert p.was_foreign is False


def test_authorization_with_html2text_artifacts():
    """html2text leaves '|' and table rules — parser should ignore them."""
    body = (
        "| | --- | | | | # Hi ALEX, | | "
        "There was an authorization without the credit card present "
        "for $25.99 at AMAZON.CA*MK7P3 on account 4111*****111**** at 2:30 pm ET."
    )
    p = parse_scotiabank_body(body, subject="Authorization without credit card present")
    assert p is not None
    assert p.amount == 25.99
    assert p.merchant_raw == "AMAZON.CA*MK7P3"
    assert p.status == "pending"


def test_foreign_indicator():
    body = (
        "There was an authorization for $42.00 at SHOPIFY outside of Canada "
        "on account 4111*****111****."
    )
    p = parse_scotiabank_body(body, subject="Authorization")
    assert p is not None
    assert p.was_foreign is True
    # 'outside' should be stripped from merchant
    assert "outside" not in p.merchant_raw.lower()


def test_amount_with_thousands_separator():
    body = (
        "There was an authorization for $1,250.00 at APPLE STORE "
        "on account 4111*****111****."
    )
    p = parse_scotiabank_body(body, subject="Authorization")
    assert p is not None
    assert p.amount == 1250.00


def test_unparseable_returns_none():
    body = "This is some marketing email that isn't a transaction notification."
    p = parse_scotiabank_body(body, subject="Newsletter")
    assert p is None


def test_status_posted():
    body = "$15.00 at TIM HORTONS posted to your account."
    p = parse_scotiabank_body(body, subject="Transaction posted")
    assert p is not None
    assert p.status == "posted"
