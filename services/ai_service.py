"""
services/ai_service.py — PDF text extraction and Claude-based data extraction.
"""
import io
import json
import logging

import pdfplumber

from config import (
    CLAUDE_MODEL,
    RETAILER_CONTACT_PERSON,
    RETAILER_EMAIL,
    RETAILER_NAME,
    RETAILER_PHONE_HINT,
    RETAILER_POSTAL_ADDRESS,
    RETAILER_POSTAL_ADDRESS_FULL,
    RETAILER_STREET_ADDRESS,
    RETAILER_STREET_ADDRESS_FULL,
    _get_claude,
)

logger = logging.getLogger(__name__)


def _response_text(resp) -> str:
    """The text of a Claude response, or "" if it carried no text block.

    resp.content is a list and can legitimately come back empty, so indexing
    [0] directly raises IndexError — a Python error that escapes the
    anthropic.APIError handlers in routers/uploads.py and surfaces as a blank
    500 page. routers/qc_checks.py already guards every call site this way.
    """
    if not getattr(resp, "content", None):
        return ""
    first = resp.content[0]
    return (getattr(first, "text", "") or "").strip()


def _parse_extraction_json(raw: str) -> dict | None:
    """Parse an extraction reply into a dict, or None if it isn't usable.

    Strips a ```json fence if present, then requires the result to be a JSON
    object: the callers immediately do data.get(...), so a bare list or string
    would raise AttributeError even though the JSON itself parsed fine.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        # "```json\n{...}\n```" splits to ['', 'json\n{...}\n', ''] — the body
        # is the middle piece. Guard the index: a reply that opens a fence and
        # never closes it would otherwise IndexError here.
        if len(parts) < 2:
            return None
        text = parts[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None

EXTRACTION_SCHEMA = {
    "quote_number": "string",
    "quote_valid_until": "string (date the quote is valid until)",
    "customer_name": "string (the CUSTOMER, not the retailer)",
    "contact_person": f"string (the customer's contact person only, never '{RETAILER_CONTACT_PERSON}' or the retailer's)",
    "billing_address": "string (single line, comma separated; the customer's billing address only)",
    "delivery_address": "string (single line, comma separated; the customer's delivery address only)",
    "email": f"string (the customer's email, e.g. a personal address -- never {RETAILER_EMAIL})",
    "phone": "string (the customer's phone/mobile -- never the retailer's 1300 number)",
    "retailer_name": f"string (the retailer, e.g. '{RETAILER_NAME}')",
    "retailer_contact_person": f"string (the retailer's contact person, e.g. '{RETAILER_CONTACT_PERSON}')",
    "retailer_postal_address": f"string (single line; the retailer's postal address, e.g. '{RETAILER_POSTAL_ADDRESS_FULL}')",
    "retailer_street_address": f"string (single line; the retailer's street address, e.g. '{RETAILER_STREET_ADDRESS_FULL}')",
    "retailer_phone": "string (the retailer's phone, e.g. the 1300 number)",
    "retailer_email": f"string (the retailer's email, e.g. '{RETAILER_EMAIL}')",
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


def extract_pdf_text(file_bytes: bytes) -> str:
    """
    Extract text from the PDF (skipping page 1, the marketing flyer).

    The agreement lays customer and retailer details in two side-by-side columns.
    Each page is split down the middle so the two halves are labelled separately,
    preventing field cross-contamination during extraction.
    """
    left_parts, right_parts, full_parts = [], [], []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i == 0:
                continue
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


def extract_with_claude(text: str) -> dict:
    client = _get_claude()

    system_prompt = (
        "You are a precise data extraction engine for solar agreements. "
        "The document has a CUSTOMER side and a RETAILER side. "
        "Extract ONLY the CUSTOMER's details for the customer fields. "
        f"NEVER mix in retailer details: the retailer is '{RETAILER_NAME}', "
        f"its contact person is '{RETAILER_CONTACT_PERSON}', its postal address contains '{RETAILER_POSTAL_ADDRESS}', "
        f"its street address is '{RETAILER_STREET_ADDRESS}', its email is "
        f"'{RETAILER_EMAIL}', and its phone is {RETAILER_PHONE_HINT} -- none of these belong "
        "in customer fields. The CUSTOMER section is under the 'LEFT COLUMN' heading "
        "and is the source for the customer_* fields. The RETAILER section is under the "
        "'RIGHT COLUMN' heading and IS the source for the retailer_* fields: fill "
        "retailer_name, retailer_contact_person, retailer_postal_address, "
        "retailer_street_address, retailer_phone, and retailer_email from that RIGHT "
        f"section (e.g. retailer_contact_person='{RETAILER_CONTACT_PERSON}', retailer_email='{RETAILER_EMAIL}'). "
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

    messages = [{"role": "user", "content": user_prompt}]
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=messages,
    )

    raw = _response_text(resp)
    data = _parse_extraction_json(raw)

    if data is None:
        # The model answered with prose instead of JSON — most often when a
        # PDF's extracted text is jumbled or a scan came through badly. One
        # corrective retry recovers nearly all of these, mirroring the
        # retry-once pattern _claude_check() in routers/qc_checks.py already
        # uses for checklist items.
        logger.warning(
            "Extraction reply was not usable JSON; retrying once. First reply: %r",
            raw[:500],
        )
        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content":
            "That was not valid JSON. Reply with ONLY the JSON object matching "
            "the schema — no markdown, no code fences, no commentary."})
        resp2 = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
        )
        raw2 = _response_text(resp2)
        data = _parse_extraction_json(raw2)

        if data is None:
            # Give up, but as an empty result rather than an exception. The
            # callers in routers/uploads.py already treat a result with no key
            # fields as "couldn't read this PDF" and show the user a proper
            # message; raising here would instead escape their
            # anthropic.APIError handlers (IndexError/JSONDecodeError are
            # Python errors, not Anthropic ones) and surface a blank 500 page.
            logger.error(
                "Extraction reply was not usable JSON twice; giving up. "
                "Replies: %r / %r", raw[:500], raw2[:500],
            )
            return {}

    return data
