## Submission Notes

### What I identified

The main problem in the scaffold was simple — the app was trusting the model's response
without checking it at all. Specifically:

- The prompt never told the model to return JSON, so it returned whatever it wanted
- The raw response string was returned directly to the caller with zero parsing
- There was no validation to check if the response matched the expected structure
- There was no error handling — any failure crashed the app with a 500 error

The fix needed to start here — treat model output as untrusted input, just like you
would treat data from any external source.

---

### What I fixed

**1. Better prompt**
- Rewrote the prompt to tell the model exactly what to return
- Asked for a JSON array only — no markdown, no explanation
- Spelled out the exact fields: item, amount, category
- Listed the 5 valid category values explicitly so the model has no room to improvise

**2. Parse and validate layer**
- Added a function that takes the raw model response and treats it as untrusted
- Strips markdown code fences (models add these even when you tell them not to)
- Parses the JSON and checks it is actually a list
- Validates each item using Pydantic — checks all fields exist and category is valid
- Raises a clear error if anything looks wrong

**3. Proper error handling**
- API errors (network issues, auth failures) → return 502 with a clear message
- Parse or validation failures → return 422 with the raw response included
- No more silent 500 crashes

**4. Retry with corrective prompt**
- If the first response can't be parsed, retry once
- The retry includes the bad response and asks the model to fix it
- If the retry also fails, return a structured error and stop

**5. Logging**
- Log the input receipt
- Log the raw model response on every attempt
- Log whether parsing succeeded or failed and why
- Enough context to reconstruct exactly what happened from logs alone

---

### Note on model provider

- Used Groq (llama-3.3-70b-versatile) instead of Anthropic due to API credit constraints
- The validation logic, error handling, and retry pattern are fully model-agnostic

### How I tested

- Started the server:
- uvicorn solution:app --reload

- Tested with the example input from the assignment:
- Invoke-RestMethod -Uri "http://localhost:8000/parse" -Method POST -ContentType "application/json" -Body '{"receipt_text": "Uber Eats $34.20\nAWS invoice $412.00\nOffice Depot $28.50\nDelta Airlines $890.00"}'

- Output:
items
{@{item=Uber Eats; amount=34.2; category=meals}, @{item=AWS invoice; amount=412.0; category=software}, @{item=Office Depot; amount=28.5; category=office_supplies}, @{item=Delta Airlines; amount=890.0; category=travel}}

