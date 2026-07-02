"""LLM-based merchant classification using Anthropic tool use.

V2: 'restaurant' tag renamed to 'dining out'.
V3: SG/SEA-aware taxonomy.
    - 'coffee' kept (dedicated coffee shops only: Starbucks, Tim Hortons).
    - Added 'drinks' (non-coffee drink shops: tea/bubble-tea/juice/coconut).
    - Added 'bakery' (BreadTalk-style bread/pastry shops).
    - Added 'convenience' (Cheers/7-Eleven/Lawson/FamilyMart).
    - Watsons/Guardian default to Shopping/[skincare]: user buys mostly
      cosmetics there and manually re-tags to Health/[pharmacy] when she
      actually buys medicine.
    - Prompt now leads with SG/SEA merchants and tells the LLM to default
      to Other/[] when unsure (user prefers blank-and-fix over wrong-confident).
"""

import os
from dataclasses import dataclass

from anthropic import Anthropic


CATEGORIES = [
    "Food & Drink",
    "Groceries",
    "Transport",
    "Shopping",
    "Housing & Utilities",
    "Entertainment",
    "Health",
    "Bills & Subscriptions",
    "Income & Refund",
    "Other",
]

TAGS_BY_CATEGORY: dict[str, list[str]] = {
    "Food & Drink": [
        "dining out", "takeout", "bar", "coffee", "drinks", "dessert",
        "bakery", "convenience",
    ],
    "Groceries": ["supermarket"],
    "Transport": ["taxi", "public-transit", "flight", "bike-share"],
    "Shopping": ["clothing", "electronics", "home", "skincare", "gift"],
    "Housing & Utilities": ["rent", "electricity", "water", "internet", "gas-bill"],
    "Entertainment": ["movies", "games", "activities"],
    "Health": ["pharmacy", "doctor", "dental", "fitness", "supplements", "gym"],
    "Bills & Subscriptions": ["phone", "software"],
    "Income & Refund": ["salary", "freelance", "refund", "cashback", "interest"],
    "Other": [],
}

ALL_TAGS = sorted({t for tags in TAGS_BY_CATEGORY.values() for t in tags})


_CLASSIFY_TOOL = {
    "name": "classify_merchant",
    "description": (
        "Assign a category and tags to a merchant for a personal finance tracker."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": CATEGORIES,
                "description": "Best-fit category from the fixed list.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": ALL_TAGS},
                "description": (
                    "Tags from the curated list for this category. "
                    "Empty array if none apply. Multiple allowed if all fit."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "One sentence explaining the choice.",
            },
        },
        "required": ["category", "tags", "rationale"],
    },
}


_SYSTEM_PROMPT = """You classify merchants into a fixed category + tag taxonomy for a personal finance tracker.

Context: the account holder has a Singapore-issued card (amounts in SGD) and a
Canada-issued card (amounts in CAD), with occasional regional travel in SE Asia.
Most merchants are Singaporean or other SE Asian, with some Canadian. Adjust
this paragraph to match your own cards and region.

Categories and their valid tags:
- Food & Drink: dining out, takeout, bar, coffee, drinks, dessert, bakery, convenience
  - coffee      = dedicated coffee shops (Starbucks, Tim Hortons, Second Cup)
  - drinks      = NON-coffee drink shops: tea, bubble tea, juice, coconut water
                  (KOI, Gong Cha, Hi Tea, Mr Coconut, iJOOZ)
  - bakery      = bread / pastry shops (BreadTalk-style)
  - convenience = 24h corner-store chains (Cheers, 7-Eleven, FamilyMart, Lawson)
  - dining out  = sit-down restaurants
  - takeout     = fast food / casual order-and-go (McDonald's, KFC, food-court stalls)
  - bar         = alcohol-focused
  - dessert     = ice cream, cakes, sweets-focused (NOT bakeries; NOT drink shops)
- Groceries: supermarket
  (full supermarkets only — convenience stores go in Food & Drink)
- Transport: taxi, public-transit, flight, bike-share
  (Gas stations like SHELL/PETRO-CANADA also belong here, no specific tag.)
- Shopping: clothing, electronics, home, skincare, gift
- Housing & Utilities: rent, electricity, water, internet, gas-bill
- Entertainment: movies, games, activities
- Health: pharmacy, doctor, dental, fitness, supplements, gym
- Bills & Subscriptions: phone, software
- Income & Refund: salary, freelance, refund, cashback, interest
- Other: (no tags)

Reference for common merchants:

Singapore / SE Asia:
- NTUC FAIRPRICE / COLD STORAGE / SHENG SIONG / GIANT -> Groceries, [supermarket]
- BIG C / TESCO LOTUS (Thailand) -> Groceries, [supermarket]
- CHEERS / 7-ELEVEN / FAMILYMART / LAWSON -> Food & Drink, [convenience]
- WATSONS / GUARDIAN / UNITY -> Shopping, [skincare]
  (User buys mostly skincare/cosmetics at these drugstores. She manually
   re-tags to Health/[pharmacy] only when she actually buys medicine.
   Default to skincare unless the merchant string clearly indicates a
   pharmacy-only purchase, e.g. a hospital pharmacy chain.)
- BREADTALK / BENGAWAN SOLO -> Food & Drink, [bakery]
- MR COCONUT / KOI / GONG CHA / IJOOZ / HI TEA -> Food & Drink, [drinks]
- STARBUCKS -> Food & Drink, [coffee]
- BUS/MRT / EZ-LINK / SMRT -> Transport, [public-transit]
- GRAB / GOJEK / TADA -> Transport, [taxi]
- DAISO -> Shopping, [home]
- Sit-down restaurants (HANSANG, DAPUR PADANG, etc.) -> Food & Drink, [dining out]
- McDonald's / KFC / BURGER KING / SUBWAY -> Food & Drink, [takeout]

Canadian:
- FREEDOM MOBILE / ROGERS / BELL / TELUS -> Bills & Subscriptions, [phone]
- LOBLAWS / METRO / NO FRILLS / SOBEYS / FRESHCO / WHOLE FOODS -> Groceries, [supermarket]
- TIM HORTONS / SECOND CUP -> Food & Drink, [coffee]
- UBER / LYFT -> Transport, [taxi]
- SHELL / PETRO-CANADA / ESSO / HUSKY -> Transport (no tag, gas)
- TTC / GO TRANSIT / OC TRANSPO -> Transport, [public-transit]
- AIR CANADA / WESTJET -> Transport, [flight]
- AMAZON.CA / SHEIN / ALIEXPRESS -> Shopping (no specific tag)
- NETFLIX / SPOTIFY / DISNEY+ / APPLE.COM/BILL -> Bills & Subscriptions, [software]
- SHOPPERS DRUG MART / REXALL -> Health, [pharmacy]
  (Canadian drugstores skew more pharmacy than SG ones; default to pharmacy.)
- LCBO / BEER STORE -> Food & Drink, [bar]

Rules:
- Tags MUST come from the listed tags for the chosen category. Empty array is OK.
- IMPORTANT: If you cannot confidently identify the merchant (opaque/abbreviated
  string, unknown brand, ambiguous between several categories), return
  category=Other with empty tags. The user prefers blank-and-fix-manually over
  a wrong-but-confident guess.
- When confident, classify with the most specific applicable category + tag.
- Always call the classify_merchant tool — never reply in plain text.
"""


@dataclass
class ClassifierResult:
    category: str
    tags: list[str]
    rationale: str


def classify_merchant(
    merchant_raw: str,
    amount: float | None = None,
    currency: str = "CAD",
    client: Anthropic | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> ClassifierResult:
    """Classify a merchant via LLM. Always returns a valid taxonomy entry."""
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    user_msg = f"Merchant: {merchant_raw}"
    if amount is not None:
        user_msg += f"\nAmount: {currency} {amount:.2f}"
    user_msg += "\n\nClassify using the classify_merchant tool."

    resp = client.messages.create(
        model=model,
        max_tokens=512,
        system=_SYSTEM_PROMPT,
        tools=[_CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_merchant"},
        messages=[{"role": "user", "content": user_msg}],
    )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "classify_merchant":
            data = block.input
            return ClassifierResult(
                category=data["category"],
                tags=list(data.get("tags", [])),
                rationale=data.get("rationale", ""),
            )

    raise RuntimeError(
        f"LLM did not return classify_merchant tool use. Got: {resp.content}"
    )
