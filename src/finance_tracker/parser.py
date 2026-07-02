"""Regex parsers for credit card notification emails.

Two issuers are supported, dispatched by sender in `parse_transaction()`:
  - Scotiabank (CAD card)  -> parse_scotiabank_body()
  - DBS/POSB (SGD card)    -> parse_dbs_body()

Each returns ParsedTransaction or None if the body doesn't match a known format.
Caller (ingest) treats None as parse_failure and logs it.
"""

import re
from dataclasses import dataclass


@dataclass
class ParsedTransaction:
    amount: float
    currency: str          # ISO 4217 read from the email (e.g. 'CAD', 'SGD')
    merchant_raw: str
    status: str            # 'pending' | 'posted'
    was_foreign: bool


# Patterns for different Scotiabank email formats
# Authorization: "for $5.65 at FREEDOM MOBILE on account 4111*****111****"
_RE_AUTH = re.compile(
    r"for\s+\$([\d,]+\.\d{2})\s+at\s+(.+?)\s+on\s+account",
    re.IGNORECASE,
)
# Posted (best-effort, multiple patterns):
_RE_POSTED_PATTERNS = [
    re.compile(
        r"\$([\d,]+\.\d{2})\s+(?:at|to|from)\s+(.+?)\s+(?:posted|was\s+posted|has\s+posted)",
        re.IGNORECASE,
    ),
    re.compile(
        r"posted\s+(?:a\s+)?(?:transaction|charge|purchase)\s+of\s+\$([\d,]+\.\d{2})\s+(?:at|to)\s+(.+?)(?:\.|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\$([\d,]+\.\d{2})\s+(?:at|to)\s+(.+?)\s+on\s+account",
        re.IGNORECASE,
    ),
]
# Foreign indicator
_RE_FOREIGN = re.compile(r"outside\s+of\s+canada", re.IGNORECASE)


def _flatten(text: str) -> str:
    """Replace newlines and html2text table chars with spaces, collapse whitespace."""
    text = text.replace("\n", " ").replace("\r", " ")
    text = text.replace("|", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _detect_status(subject: str, body: str) -> str:
    """Determine 'pending' vs 'posted' from subject + body."""
    haystack = (subject + " " + body).lower()
    if "authorization" in haystack or "authorisation" in haystack:
        return "pending"
    if "posted" in haystack or "transaction posted" in haystack:
        return "posted"
    # Default to pending — Scotiabank sends authorizations more frequently
    return "pending"


def parse_scotiabank_body(
    body: str, subject: str = ""
) -> ParsedTransaction | None:
    """Parse a Scotiabank email body. Returns None if no match."""
    text = _flatten(body)
    status = _detect_status(subject, text)

    # Try the right pattern first based on status, then fall back
    if status == "pending":
        candidates = [_RE_AUTH] + _RE_POSTED_PATTERNS
    else:
        candidates = _RE_POSTED_PATTERNS + [_RE_AUTH]

    match = None
    for pattern in candidates:
        m = pattern.search(text)
        if m:
            match = m
            break
    if match is None:
        return None

    amount_str = match.group(1).replace(",", "")
    try:
        amount = float(amount_str)
    except ValueError:
        return None

    merchant_raw = match.group(2).strip()
    # Trim trailing words that often leak in: "outside of Canada", "at 9:53 am"
    merchant_raw = re.sub(r"\s+(at|outside|on)\s+.*$", "", merchant_raw, flags=re.IGNORECASE)
    merchant_raw = merchant_raw.strip(" .,;:")

    if not merchant_raw:
        return None

    was_foreign = bool(_RE_FOREIGN.search(text))
    currency = "CAD"  # Scotiabank emails always show CAD

    return ParsedTransaction(
        amount=amount,
        currency=currency,
        merchant_raw=merchant_raw,
        status=status,
        was_foreign=was_foreign,
    )


# ---------------------------------------------------------------------------
# DBS / POSB emails (Singapore, SGD)
# ---------------------------------------------------------------------------
#
# Two ingestable shapes, both ending in SGD spend on this card:
#
# 1) Card purchase ("Card Transaction Alert"):
#      Date & Time: 05 Jun 11:10 (SGT)  Amount: SGD3.20
#      From: DBS/POSB card ending 1234  To: FAIRPRICE FINEST
#      If unauthorized, please call our DBS hotline. ...
#
# 2) Outgoing transfer ("iBanking Alerts", e.g. PayNow / FAST):
#      We refer to your PAYNOW dated 03 Jun ... the transaction was completed.
#      Amount: SGD50.00
#      From: DBS Multiplier Account A/C ending 5678  To: JANE TAN (MOBILE ending 9876)
#      If unauthorised, please call our DBS hotline. ...
#
# Both carry "Amount: <CUR><n>" + "To: <recipient>", so one parser handles both.
# "Amount: SGD3.20" carries the ISO currency prefix; for overseas spend DBS shows
# the foreign currency directly (e.g. "Amount: THB100.00"), so we read currency
# from the email rather than assuming SGD.
#
# The same Gmail filter also catches non-transaction iBanking alerts (OTP, token
# setup, PayNow *registration*, Manage-Alert) which carry no Amount line, and
# *incoming* transfers (From is a third party, not the user's account) which are
# income — both are left for is_ingestable to skip / a future income parser.
DBS_HOME_CURRENCY = "SGD"

_RE_DBS_AMOUNT = re.compile(
    r"Amount:\s*([A-Z]{3})\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)
# Recipient sits in the "To:" field, terminated by the boilerplate that follows.
_RE_DBS_MERCHANT = re.compile(
    r"To:\s*(.+?)\s+(?:If\s+unauthori[sz]ed|Thank\s+you\s+for\s+banking)",
    re.IGNORECASE,
)
# Outgoing money: the "From:" account is one of the user's own DBS/POSB accounts.
# Incoming transfers show a third-party name here instead, so this excludes them.
_RE_DBS_FROM_OWN = re.compile(r"From:\s*(?:DBS|POSB)\b", re.IGNORECASE)


def dbs_email_kind(subject: str, body: str) -> str | None:
    """Classify a DBS email as an ingestable expense, or None to skip.

    Returns 'card' for a card purchase, 'outgoing' for a transfer leaving one of
    the user's own DBS accounts (PayNow/FAST → an expense), or None for
    non-transaction alerts and incoming transfers (handled by a future income
    parser).
    """
    flat = _flatten(body)
    if "card transaction alert" in (subject + " " + flat).lower():
        return "card"
    if (
        _RE_DBS_AMOUNT.search(flat)
        and _RE_DBS_MERCHANT.search(flat)
        and _RE_DBS_FROM_OWN.search(flat)
    ):
        return "outgoing"
    return None


def parse_dbs_body(body: str, subject: str = "") -> ParsedTransaction | None:
    """Parse a DBS card purchase or outgoing transfer. None if no match."""
    text = _flatten(body)

    amount_match = _RE_DBS_AMOUNT.search(text)
    if amount_match is None:
        return None

    currency = amount_match.group(1).upper()
    try:
        amount = float(amount_match.group(2).replace(",", ""))
    except ValueError:
        return None

    merchant_match = _RE_DBS_MERCHANT.search(text)
    if merchant_match is None:
        return None
    merchant_raw = merchant_match.group(1).strip(" .,;:")
    if not merchant_raw:
        return None

    # DBS alerts confirm a completed transaction ("the transaction was
    # completed"), so they post immediately — no separate pending stage.
    return ParsedTransaction(
        amount=amount,
        currency=currency,
        merchant_raw=merchant_raw,
        status="posted",
        was_foreign=currency != DBS_HOME_CURRENCY,
    )


# ---------------------------------------------------------------------------
# Dispatch by sender
# ---------------------------------------------------------------------------


def is_ingestable(sender: str, subject: str, body: str) -> bool:
    """False for known non-transaction notification emails.

    The DBS Gmail filter also catches account-level "iBanking Alerts" (OTP,
    digital-token setup, PayNow registration, alert-management confirmations)
    and incoming transfers — none of which are expenses on this card. We ingest
    DBS card purchases and outgoing transfers (PayNow/FAST); see dbs_email_kind.
    """
    if "dbs.com" in (sender or "").lower():
        return dbs_email_kind(subject, body) is not None
    return True  # Scotiabank: let the parser decide via regex match


def parse_transaction(
    sender: str, body: str, subject: str = ""
) -> ParsedTransaction | None:
    """Pick the right issuer parser based on the email sender."""
    if "dbs.com" in (sender or "").lower():
        return parse_dbs_body(body, subject)
    return parse_scotiabank_body(body, subject)
