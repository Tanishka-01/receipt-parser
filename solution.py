import json
import logging
import re
from typing import Literal

from fastapi import FastAPI, HTTPException
from groq import Groq, APIConnectionError, APIStatusError
from pydantic import BaseModel, ValidationError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("receipt_parser")

# ---------------------------------------------------------------------------
# App + client
# ---------------------------------------------------------------------------

app = FastAPI()
client = Groq()

MODEL = "llama-3.3-70b-versatile"
MAX_RETRIES = 2

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a receipt parsing assistant. Extract every line item from the receipt and return ONLY a JSON array. No markdown, no code fences, no explanation — raw JSON only.

Each object in the array must have exactly these fields:
- "item": string — the name of the item or service
- "amount": float — the numeric cost, no currency symbols
- "category": string — must be exactly one of: meals, travel, software, office_supplies, other

Rules:
- If a line item has no clear amount, omit it.
- If you cannot determine a category, use "other".
- Return [] if no line items are found.
- Do not include tax, tips, or totals as line items."""

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

VALID_CATEGORIES = Literal["meals", "travel", "software", "office_supplies", "other"]


class ReceiptItem(BaseModel):
    item: str
    amount: float
    category: VALID_CATEGORIES


class ReceiptRequest(BaseModel):
    receipt_text: str


class ParsedReceiptResponse(BaseModel):
    items: list[ReceiptItem]
    attempts: int

# ---------------------------------------------------------------------------
# parse_and_validate
# ---------------------------------------------------------------------------

def parse_and_validate(raw: str) -> list[ReceiptItem]:
    """
    1. Strip markdown code fences if present.
    2. Parse as JSON.
    3. Assert result is a list.
    4. Validate each item with Pydantic.
    Raises ValueError or TypeError with a descriptive message on any failure.
    """
    # Step 1: Strip code fences
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)

    # Step 2: Parse JSON
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model response is not valid JSON: {e}") from e

    # Step 3: Must be a list
    if not isinstance(parsed, list):
        raise TypeError(
            f"Expected a JSON array, got {type(parsed).__name__}. "
            f"Response was: {stripped[:200]}"
        ) 

    # Step 4: Validate each item
    validated = []
    for i, entry in enumerate(parsed):
        try:
            validated.append(ReceiptItem(**entry))
        except ValidationError as e:
            raise ValueError(f"Item at index {i} failed validation: {e}") from e

    return validated

# ---------------------------------------------------------------------------
# Model call helper
# ---------------------------------------------------------------------------

def call_model(messages: list[dict]) -> str:
    """Call Groq and return the raw response text. Raises on API errors."""
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        temperature=0,
    )
    return response.choices[0].message.content

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/parse", response_model=ParsedReceiptResponse)
def parse_receipt(request: ReceiptRequest):
    logger.info("Received receipt for parsing (length=%d chars)", len(request.receipt_text))
    logger.debug("Receipt text: %s", request.receipt_text)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Parse this receipt and categorize each item:\n\n{request.receipt_text}",
        },
    ]

    last_parse_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("Attempt %d / %d", attempt, MAX_RETRIES)

        # ── 1. Call the model ──────────────────────────────────────────────
        try:
            raw = call_model(messages)
        except APIConnectionError as e:
            logger.error("Attempt %d: network error calling Groq: %s", attempt, e)
            raise HTTPException(
                status_code=502,
                detail=f"Upstream API connection error: {e}",
            ) from e
        except APIStatusError as e:
            logger.error(
                "Attempt %d: Groq API error (status=%d): %s",
                attempt, e.status_code, e.message,
            )
            raise HTTPException(
                status_code=502,
                detail=f"Upstream API error (status {e.status_code}): {e.message}",
            ) from e

        logger.info("Attempt %d: raw model response received", attempt)
        logger.debug("Attempt %d: raw response: %s", attempt, raw)

        # ── 2. Parse and validate ──────────────────────────────────────────
        try:
            items = parse_and_validate(raw)
            logger.info("Attempt %d: parse and validation succeeded (%d items)", attempt, len(items))
            return ParsedReceiptResponse(items=items, attempts=attempt)

        except (ValueError, TypeError) as e:
            last_parse_error = e
            logger.warning(
                "Attempt %d: parse/validation failed — %s", attempt, e
            )
            logger.debug("Attempt %d: bad response was: %s", attempt, raw[:500])

            # ── 3. Build corrective retry if attempts remain ───────────────
            if attempt < MAX_RETRIES:
                logger.info("Attempt %d: building corrective retry prompt", attempt)
                correction_message = {
                    "role": "user",
                    "content": (
                        f"Your previous response could not be used. Error: {e}\n\n"
                        f"Your response was:\n{raw}\n\n"
                        "Return ONLY a raw JSON array with no markdown, no code fences, "
                        "and no explanation. Each object must have: "
                        "\"item\" (string), \"amount\" (float), "
                        "\"category\" (one of: meals, travel, software, office_supplies, other)."
                    ),
                }
                # Append the bad assistant turn + correction instruction so
                # the model has full context of what it returned and why it failed.
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    correction_message,
                ]

    # All attempts exhausted
    logger.error(
        "All %d attempts failed. Last error: %s", MAX_RETRIES, last_parse_error
    )
    raise HTTPException(
        status_code=422,
        detail=f"Model response failed validation after {MAX_RETRIES} attempts: {last_parse_error}",
    )