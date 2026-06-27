"""
services/claude.py — PDF text extraction and Claude-based data extraction.

Contains:
  - EXTRACTION_SCHEMA  : field descriptions sent to Claude
  - extract_pdf_text() : split-column PDF text extraction via pdfplumber
  - extract_with_claude(): structured JSON extraction via the Anthropic API
"""
import io
import json

import pdfplumber

from core import CLAUDE_MODEL, _get_claude

# ---------------------------------------------------------------------------
# Schema sent to Claude for structured extraction
# ---------------------------------------------------------------------------
EXTRACTION_SCHEMA = {
    "quote_number": "string",
    "quote_valid_until": "string (date the quote is valid until)",
    "customer_name": "string (the CUSTOMER, not the retailer)",
    "contact_person": "string (the customer's contact person only, never 'Nik' or the retailer's)",
    "billing_address": "string (single line, comma separated; the customer's billing address only)",
    "delivery_address": "string (single line, comma separated; the customer's delivery address only)",
    "email": "string (the customer's email, e.g. a personal address -- never sales@adssolar.com.au)",
    "phone": "string (the customer's phone/mobile -- never the retailer's 1300 number)",
    "retailer_name": "string (the retailer, e.g. 'ADS Pty Ltd t/as ADS Solar')",
    "retailer_contact_person": "string (the retailer's contact person, e.g. 'Nik')",
    "retailer_postal_address": "string (single line; the retailer's postal address, e.g. 'PO Box 6208, Norwest NSW 2153')",
    "retailer_street_address": "string (single line; the retailer's street address, e.g. '104, 29-31 Solent Circuit, Baulkham Hills NSW 2153')",
    "retailer_phone": "string (the retailer's phone, e.g. the 1300 number)",
    "retailer_email": "string (the retailer's email, e.g. 'sales@adssolar.com.au')",
    "roof_type": "string (the roof type, e.g. 'Tiled Roof', 'Colorbond', 'Metal' — look for the 'Roof Type' row in the system/pricing table and read its specification text)",
    "line_items": (
        "array of objects covering EVERY row of the System/pricing table in order, "
        "INCLUDING the equipment rows (Panels, Inverter, Inverter phase, Racking, "
        "Optimisers, Exp ctrl device, Battery, Stories, Extended Warranty, Options, "
        "Roof Type) AND the summary/financial rows (System Price, Less STC incentive, "
        "VIC Interest free Loan, VIC Rebate, ACT Govt Next Gen Rebate, Battery Rebate, "
        "Total Price). Each object: {\"item\": string (the row label, e.g. 'Panels', "
        "'System Price', 'Less STC incentive', 'VIC Rebate', 'Total Price'), "
        "\"quantity\": string (if the specification text starts with a pattern like "
        "'1.0 X', '26.0 X', etc., extract that number dropping the trailing '.0' — "
        "e.g. '26' for '26.0 X JKM510N...', '1' for '1.0 X [Solar Vic Rebate]...', "
        "'1' for '1.0 X [STC Battery Rebate]...'. This applies to ALL rows including "
        "rebate and financial rows — if the spec starts with '<number> X', capture it. "
        "Leave empty ONLY if the row has no specification text at all, e.g. "
        "'System Price', 'Total Price'), "
        "\"specification\": string (the full description/spec text WITHOUT the leading "
        "'<number> X ' quantity prefix; empty if the row has no spec text at all), "
        "\"price\": string (the dollar amount shown on that row, e.g. "
        "'$ 31,742.62', '$ 3,142.62', '$ -1,400.00', '$ 21,100.00'; empty if the row has "
        "no price)}. IMPORTANT: read the prices from the 'FULL TEXT' section, where each "
        "amount appears at the END of its row -- do not leave a price blank if the FULL "
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
        "===== LEFT COLUMN - CUSTOMER SIDE (use for the customer_* fields) =====\n"
        + "\n".join(left_parts)
        + "\n\n===== RIGHT COLUMN - RETAILER SIDE (use for the retailer_* fields; "
          "do NOT use for customer fields) =====\n"
        + "\n".join(right_parts)
        + "\n\n===== FULL TEXT (use ONLY for pricing/dates) =====\n"
        + "\n".join(full_parts)
    )


# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------
def extract_with_claude(text: str) -> dict:
    client = _get_claude()

    system_prompt = (
        "You are a precise data extraction engine for solar agreements. "
        "The document has a CUSTOMER side and a RETAILER side. "
        "Extract ONLY the CUSTOMER's details for the customer fields. "
        "NEVER mix in retailer details: the retailer is 'ADS Pty Ltd t/as ADS Solar', "
        "its contact person is 'Nik', its postal address contains 'PO Box 6208 / Norwest', "
        "its street address is 'Solent Circuit, Baulkham Hills', its email is "
        "'sales@adssolar.com.au', and its phone is a 1300 number -- none of these belong "
        "in customer fields. The CUSTOMER section is under the 'LEFT COLUMN' heading "
        "and is the source for the customer_* fields. The RETAILER section is under the "
        "'RIGHT COLUMN' heading and IS the source for the retailer_* fields: fill "
        "retailer_name, retailer_contact_person, retailer_postal_address, "
        "retailer_street_address, retailer_phone, and retailer_email from that RIGHT "
        "section (e.g. retailer_contact_person='Nik', retailer_email='sales@adssolar.com.au'). "
        "Do NOT use the RIGHT section for any customer field, and do NOT use the LEFT "
        "section for any retailer field. For the line_items pricing table, take the PRICE for "
        "each row from the 'FULL TEXT' section (the column-split text clips the price "
        "column, so prices may be missing there) -- every row that shows a dollar amount "
        "in the FULL TEXT must carry that amount in its 'price'. "
        "Return ONLY a valid JSON object matching the schema, "
        "with no markdown, no code fences, and no commentary. If a field is missing, "
        "use an empty string."
    )

    user_prompt = (
        f"Schema (keys and expected meaning):\n{json.dumps(EXTRACTION_SCHEMA, indent=2)}\n\n"
        f'Agreement text:\n"""\n{text}\n"""'
    )

    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = resp.content[0].text.strip()
    # Strip markdown code fences if Claude wrapped the JSON
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()
    return json.loads(raw)
