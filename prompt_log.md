
Claude finished the response
Here is the broken scaffold I am working with:
from fastapi import FastAPI
from pydantic import BaseModel
import anthropic
app = FastAPI()
client = anthropic.Anthropic()
class ReceiptRequest(BaseModel):
    receipt_text: str
@app.post("/parse")
def parse_receipt(request: ReceiptRequest):
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": f"Parse this receipt and categorize each item: {request.receipt_text}"
            }
        ]
    )
    return {"result": message.content[0].text}
I have already identified the core problem — raw model output is returned directly with no parsing, no validation, and no error handling. The prompt also gives the model no instruction on what format to return. Don't write any code yet, just confirm you understand the problem.


Confirmed. The three issues are:

No output format instruction — the prompt asks the model to "parse and categorize" but doesn't tell it how to structure the response (JSON, what fields, what categories). The model will return freeform prose that's unpredictable and not machine-readable.
No parsing or validation — message.content[0].text is returned raw. Even if the model happens to return JSON-like text, nothing parses it into a structured object or validates that required fields are present.
No error handling — if the Anthropic call fails, if the response is malformed, or if content[0] doesn't exist, the endpoint crashes with an unhandled exception rather than returning a clean error response.
The fix will involve: a structured prompt that specifies exact JSON schema, JSON parsing of the response, Pydantic models for the output shape, and try/except blocks around the API call and parse step. Ready when you are.

Help me design the system prompt first — it should tell the model to return ONLY a JSON array with these fields: item (string), amount (float), category (one of: meals, travel, software, office_supplies, other). No markdown, no explanation, no code fences.


Here's the system prompt:

You are a receipt parsing assistant. Extract every line item from the receipt and return ONLY a JSON array. No markdown, no code fences, no explanation — raw JSON only.

Each object in the array must have exactly these fields:
- "item": string — the name of the item or service
- "amount": float — the numeric cost, no currency symbols
- "category": string — must be exactly one of: meals, travel, software, office_supplies, other

Rules:
- If a line item has no clear amount, omit it.
- If you cannot determine a category, use "other".
- Return [] if no line items are found.
- Do not include tax, tips, or totals as line items.
A few design decisions worth noting:

"raw JSON only" is stated twice — once at the top and once implicitly through the fence/explanation prohibition. Repetition here reduces hallucination of markdown wrapping.
The category list uses exact strings so you can validate against an enum without any normalization logic on your end.
The edge case rules (no amount, unknown category, empty receipt, tax/tips) prevent the model from making ad-hoc decisions that would break your schema or pollute the data.
[] for empty gives your parser a consistent return type rather than a null or an explanatory string.
Want to wire this into the endpoint next?

Now write a Python function called parse_and_validate that takes the raw model response string and does this: 1. Strips markdown code fences if present 2. Parses it as JSON 3. Checks the result is actually a list, not a dict or anything else 4. Validates each item using Pydantic with fields: item (str), amount (float), category (must be one of: meals, travel, software, office_supplies, other) Raise a clear error at each step if something goes wrong.


python
import json
from pydantic import BaseModel, ValidationError
from typing import Literal
import re

VALID_CATEGORIES = Literal["meals", "travel", "software", "office_supplies", "other"]

class ReceiptItem(BaseModel):
    item: str
    amount: float
    category: VALID_CATEGORIES

def parse_and_validate(raw: str) -> list[ReceiptItem]:
    # Step 1: Strip markdown code fences if present
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)

    # Step 2: Parse as JSON
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"Model response is not valid JSON: {e}") from e

    # Step 3: Check the result is a list
    if not isinstance(parsed, list):
        raise TypeError(
            f"Expected a JSON array, got {type(parsed).__name__}. "
            f"Response was: {stripped[:200]}"
        )

    # Step 4: Validate each item with Pydantic
    validated = []
    for i, entry in enumerate(parsed):
        try:
            validated.append(ReceiptItem(**entry))
        except ValidationError as e:
            raise ValueError(f"Item at index {i} failed validation: {e}") from e

    return validated
A few notes on the decisions made:

The regex in step 1 handles ```json, plain ```, and any surrounding whitespace — covers the most common fence variants the model might emit despite instructions.
The from e chaining on all raises preserves the original traceback, which matters when you're debugging what the model actually returned.
The TypeError vs ValueError split is intentional — a wrong type and a malformed value are meaningfully different failure modes, and you may want to catch them separately in the endpoint.
The truncated response in the TypeError message ([:200]) gives you diagnostic context without flooding logs with a huge payload.
Pydantic handles coercion — if the model returns "amount": "12.50" as a string, Pydantic will cast it to float rather than reject it, which is the right behavior for this use case.
Ready to wire this into the FastAPI endpoint with error handling?

Now in the FastAPI endpoint I want to handle these failure cases explicitly instead of letting them crash as 500 errors: 1. The AI API call itself fails (network error, auth error) 2. The response comes back but cannot be parsed as JSON 3. The response parses but fails schema validation What HTTP status codes should I return for each case and why?


Good question to settle before writing the handler. Here's the reasoning for each:

1. AI API call fails (network error, auth error) → 502 Bad Gateway

Your FastAPI service is acting as a proxy to an upstream dependency (Anthropic). When that dependency fails — not your code — 502 is the correct signal. It tells the caller "I received your request fine, but something broke between me and a service I depend on." A 500 would imply the fault is in your own server logic, which is misleading.

2. Response comes back but can't be parsed as JSON → 502 Bad Gateway

Same upstream responsibility. The Anthropic call technically succeeded in the network sense, but the model returned something your contract requires to be JSON and it isn't. The failure is still attributable to the upstream response, not to anything the caller did wrong. Some teams use 500 here, but 502 is more honest — you got a bad response from a dependency.

3. Parses but fails schema validation → 422 Unprocessable Entity

This one is subtle. The JSON parsed fine, but the shape of the data doesn't match your expected schema — missing fields, wrong types, invalid category string. This is semantically identical to what FastAPI itself returns when request validation fails. The model is effectively an untrusted data source and its output failed your contract. 422 signals "the structure of what I received was not processable" without implying a network fault.

Summary table:

Failure	Status	Reason
Anthropic SDK throws	502	Upstream dependency failure
Response is not valid JSON	502	Upstream returned unusable response
JSON parses but schema invalid	422	Data shape doesn't meet contract
One practical addition worth building in: log the raw model response before raising on cases 2 and 3. Once this is in production you'll want to be able to see what the model returned when things go wrong, not just that they did.

Ready to write the endpoint now?

if the first model response cannot be parsed, should I retry with a fresh prompt or include the bad response in the retry and ask the model to correct it? What is more reliable and why?


Include the bad response and ask the model to correct it. Here's why:

The core reason: context reduces guesswork

A fresh retry gives the model no information about what went wrong. It will regenerate from scratch and has a reasonable chance of making the same mistake again — especially if something in the receipt text is ambiguous or unusual. Including the bad response tells the model exactly what it produced and what was wrong with it, which is a much tighter constraint.

What the correction prompt should contain

Three things:

The original receipt text (so it doesn't lose context)
The bad response it returned verbatim
A precise, specific error — not "that was wrong" but "this is not valid JSON" or "item at index 2 is missing the amount field"
The specificity matters. "Fix your response" is vague. "Your response contained a markdown code fence — return raw JSON only, no backticks" gives the model an actionable correction.

Where a fresh retry is better

If the failure looks like a hallucination or total format collapse — e.g. the model returned a paragraph of prose instead of anything JSON-like — a correction prompt may just produce a polished version of the same wrong thing. In that case a fresh prompt with a reinforced instruction ("return ONLY a raw JSON array, no other text") is more likely to reset the behavior.

Practical recommendation

Use a two-stage strategy:

Attempt 1: normal prompt + system prompt
Attempt 2: include bad response + specific error message, ask for correction
Attempt 3 (if you need one): fresh prompt with reinforced format instruction, no bad response
This way you get the diagnostic benefit of a correction on the first retry, and fall back to a clean reset only if correction fails. Cap at 2-3 attempts total — more than that and you're masking a systematic prompt problem that needs to be fixed, not retried past.

Ready to write the full endpoint with retry logic now?

Now put it all together into the final complete file. Use:
- The system prompt we designed
- The parse_and_validate function we wrote
- Explicit error handling with 502 for API errors and 422 for parse/validation failures
- A corrective retry that sends the bad response back with a specific error message
- Logging at every step: the input, the raw model response, which attempt succeeded or failed, and what went wrong
- Use Groq (llama-3.3-70b-versatile) instead of Anthropic as the model provider



Viewed a file, created a file, read a file
Viewed a file, created a file, read a file
A few things worth calling out about how the pieces connect:

Retry conversation structure. The corrective retry appends both the bad assistant turn and the correction instruction to the message history. This means the model sees: original system prompt → original user request → what it returned → exactly what was wrong. That's the full context it needs to self-correct without guessing.

API errors always raise immediately. Network and auth errors don't enter the retry loop — they raise 502 right away. Retrying a broken connection or a 401 is pointless and would just add latency before the same failure.

temperature=0. Set deliberately so the model is deterministic. On a corrective retry you want it to follow the correction instruction, not introduce new variation.

attempts in the response. The response payload includes how many attempts it took. Useful for monitoring — if you're regularly seeing attempts: 2 in production, the system prompt needs tuning.

Install deps:
bash
pip install fastapi groq pydantic uvicorn
Run:

bash
uvicorn receipt_parser:app --reload

Receipt parser
PY 


