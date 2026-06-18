import os
import io
import json

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pdfplumber
from groq import Groq
from groq import AuthenticationError, GroqError

import db  # database layer (auto-creates extractions.db on import)

# ---------------------------------------------------------------------------
# Config — loaded from .env
# ---------------------------------------------------------------------------
load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

app = FastAPI(title="Solar Agreement Extractor")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# Serve a small inline SVG favicon so the browser stops requesting /favicon.ico
# (which would otherwise 404 on every page load).
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="none" stroke="#e8590c" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2'
    'M6.3 17.7l-1.4 1.4M19.1 4.9l-1.4 1.4"/></svg>'
)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=_FAVICON, media_type="image/svg+xml")


# ---------------------------------------------------------------------------
# PDF text extraction
# ---------------------------------------------------------------------------
def extract_pdf_text(file_bytes: bytes) -> str:
    """
    Extract text from the PDF (skipping page 1, the marketing flyer).

    The agreement lays customer details and retailer details in two
    side-by-side columns. A plain extract_text() reads each row left-to-right
    and glues the two columns together (e.g. the customer's billing address
    ends up merged with the retailer's postal address). To avoid that, each
    page is split down the middle and the left (customer) and right (retailer)
    halves are extracted separately and clearly labelled.
    """
    left_parts, right_parts, full_parts = [], [], []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i == 0:
                continue  # ignore page one per requirement
            mid = page.width / 2
            left = page.crop((0, 0, mid, page.height)).extract_text() or ""
            right = page.crop((mid, 0, page.width, page.height)).extract_text() or ""
            left_parts.append(left)
            right_parts.append(right)
            full_parts.append(page.extract_text() or "")

    return (
        "===== LEFT COLUMN (CUSTOMER SIDE) =====\n"
        + "\n".join(left_parts)
        + "\n\n===== RIGHT COLUMN (RETAILER SIDE — do NOT use for customer fields) =====\n"
        + "\n".join(right_parts)
        + "\n\n===== FULL TEXT (use ONLY for pricing/dates, NOT for customer vs retailer) =====\n"
        + "\n".join(full_parts)
    )


# ---------------------------------------------------------------------------
# Groq extraction
# ---------------------------------------------------------------------------
EXTRACTION_SCHEMA = {
    "quote_number": "string",
    "quote_valid_until": "string (date the quote is valid until)",
    "customer_name": "string (the CUSTOMER, not the retailer)",
    "contact_person": "string (the customer's contact person only, never 'Nik' or the retailer's)",
    "billing_address": "string (single line, comma separated; the customer's billing address only)",
    "delivery_address": "string (single line, comma separated; the customer's delivery address only)",
    "email": "string (the customer's email, e.g. a personal address — never sales@adssolar.com.au)",
    "phone": "string (the customer's phone/mobile — never the retailer's 1300 number)",
    "retailer_name": "string (the retailer, e.g. 'ADS Pty Ltd t/as ADS Solar')",
    "retailer_contact_person": "string (the retailer's contact person, e.g. 'Nik')",
    "retailer_postal_address": "string (single line; the retailer's postal address, e.g. 'PO Box 6208, Norwest NSW 2153')",
    "retailer_street_address": "string (single line; the retailer's street address, e.g. '104, 29-31 Solent Circuit, Baulkham Hills NSW 2153')",
    "retailer_phone": "string (the retailer's phone, e.g. the 1300 number)",
    "retailer_email": "string (the retailer's email, e.g. 'sales@adssolar.com.au')",
    "roof_type": "string",
    "line_items": (
        "array of objects covering EVERY row of the System/pricing table in order, "
        "INCLUDING the equipment rows (Panels, Inverter, Inverter phase, Racking, "
        "Optimisers, Exp ctrl device, Battery, Stories, Extended Warranty, Options, "
        "Roof Type) AND the summary/financial rows (System Price, Less STC incentive, "
        "VIC Interest free Loan, VIC Rebate, ACT Govt Next Gen Rebate, Battery Rebate, "
        "Total Price). Each object: {\"item\": string (the row label, e.g. 'Panels', "
        "'System Price', 'Less STC incentive', 'VIC Rebate', 'Total Price'), "
        "\"quantity\": string (the number before the 'X' at the start of the specification, "
        "e.g. '26' for '26.0 X JKM510N...', '1' for '1.0 X FOXESS...'; drop the trailing "
        "'.0'; empty for summary/financial rows or rows with no quantity like 'Roof Type'), "
        "\"specification\": string (the description/spec text WITHOUT the leading "
        "'<number> X ' quantity prefix, or empty for summary rows that have none), "
        "\"price\": string (the dollar amount shown on that row, e.g. "
        "'$ 31,742.62', '$ 3,142.62', '$ -1,400.00', '$ 21,100.00'; empty if the row has "
        "no price)}. IMPORTANT: read the prices from the 'FULL TEXT' section, where each "
        "amount appears at the END of its row — do not leave a price blank if the FULL "
        "TEXT shows a dollar amount for that row."
    ),
    "system_price": "string",
    "stc_incentive": "string (the 'Less STC incentive' amount)",
    "vic_rebate": "string",
    "battery_rebate": "string",
    "total_price": "string",
    "deposit": "string",
    "balance": "string",
    "payment_terms": "string",
    "install_date": "string (proposed install date)",
    "balance_due_date": "string",
    "notes": "string (any notes, e.g. gift voucher)",
}


def extract_with_groq(text: str) -> dict:
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY not set. Add it to your .env file.")

    client = Groq(api_key=GROQ_API_KEY)

    system_prompt = (
        "You are a precise data extraction engine for solar agreements. "
        "The document has a CUSTOMER side and a RETAILER side. "
        "Extract ONLY the CUSTOMER's details for the customer fields. "
        "NEVER mix in retailer details: the retailer is 'ADS Pty Ltd t/as ADS Solar', "
        "its contact person is 'Nik', its postal address contains 'PO Box 6208 / Norwest', "
        "its street address is 'Solent Circuit, Baulkham Hills', its email is "
        "'sales@adssolar.com.au', and its phone is a 1300 number — none of these belong "
        "in customer fields. The CUSTOMER section is under the 'LEFT COLUMN' heading; "
        "the RETAILER section is under the 'RIGHT COLUMN' heading and must not be used "
        "for any customer field. For the line_items pricing table, take the PRICE for "
        "each row from the 'FULL TEXT' section (the column-split text clips the price "
        "column, so prices may be missing there) — every row that shows a dollar amount "
        "in the FULL TEXT must carry that amount in its 'price'. "
        "Return ONLY a valid JSON object matching the schema, "
        "with no markdown, no code fences, and no commentary. If a field is missing, "
        "use an empty string."
    )

    user_prompt = (
        f"Schema (keys and expected meaning):\n{json.dumps(EXTRACTION_SCHEMA, indent=2)}\n\n"
        f'Agreement text:\n"""\n{text}\n"""'
    )

    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    return json.loads(resp.choices[0].message.content)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    records = db.get_recent()
    return templates.TemplateResponse(
        request, "index.html", {"records": records, "result": None, "error": None}
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file.")

    file_bytes = await file.read()
    text = extract_pdf_text(file_bytes)

    try:
        data = extract_with_groq(text)
    except AuthenticationError:
        records = db.get_recent()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "records": records,
                "result": None,
                "error": "Invalid Groq API key. Check the GROQ_API_KEY value in your .env file "
                         "(it should start with 'gsk_') and restart the server.",
            },
        )
    except GroqError as e:
        records = db.get_recent()
        return templates.TemplateResponse(
            request,
            "index.html",
            {"records": records, "result": None, "error": f"Groq error: {e}"},
        )

    db.save_extraction(file.filename, data)
    records = db.get_recent()

    return templates.TemplateResponse(
        request, "index.html", {"records": records, "result": data}
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)