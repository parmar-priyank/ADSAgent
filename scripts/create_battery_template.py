"""
One-off script: create a new "Battery" Pre-QC template by copying the
confirmed subset of items from the existing "Checklist" Pre-QC template,
preserving reference/prompt text exactly (no rewriting).

Run this ONCE on the server, from the app's own directory, with its venv:
    cd /var/www/adsagent
    venv/bin/python3 scripts/create_battery_template.py

It only reads from "Checklist" and inserts a new template — it does not
touch the existing "Checklist" template at all.
"""
import sys
sys.path.insert(0, ".")

import db.checklist_repo as tdb

SOURCE_TEMPLATE_NAME = "Checklist"
SOURCE_KIND = "pre"
NEW_TEMPLATE_NAME = "Battery"

# Items to KEEP for the Battery template — matched by exact text against the
# source template's rows. Everything in the source NOT listed here is a
# panel-specific item and is left out of the Battery template entirely.
KEEP_TEXTS = {
    "Signed Agreement",
    "HBCF Insurance (If Required)",
    "Deposit",
    "Meter Photo/Switchboard",
    "Electricity Bill(All Pages)",
    "Rate Notice",
    "Meter Approval",
    "Welcome Email/Invoice/RL/Inverter Location/Fact sheet",
    "Finance Approved (Brighte/Plenti)",
    "Type of Installation ?",
    "Job checked by Accounts",
    "Customer informed installation date both on phone and email",
    "Wi-Fi availability",
    "Sent customer updated Invoice, and inverter location if any change then original signed agreement before installation",
    "Double check SO against Original Signed Agreement",
    "Battery model number matches with signed agreement",
    "Battery model number in signed agreement",
    "Battery model number in Packing slip",
    "Electrical Kits",
    "Organise Delivery of material if required",
    "Inform installer installation date both phone and email",
    "Raise WO for Installer",
    "Create STC in Green Deal/Bridgeselect",
    "Customer name and address matches Rates Notice, bill and Signed agreement form",
    "Battery model number matches with signed agreement and Packing Slip",
    "CECBattery Approved Date",
    "CECBattery Expiration Date",
    "Electrician License Expiration Date",
    "SAA Expiration date",
}


def main():
    templates = tdb.list_templates(kind=SOURCE_KIND)
    source_row = next((t for t in templates if t["name"] == SOURCE_TEMPLATE_NAME), None)
    if not source_row:
        print(f"ERROR: no Pre-QC template named {SOURCE_TEMPLATE_NAME!r} found.")
        print("Available Pre-QC templates:", [t["name"] for t in templates])
        return

    source = tdb.get_template(source_row["id"])
    source_items = tdb.get_items(source["id"])

    kept = [it for it in source_items if not it.get("is_section") and it["text"] in KEEP_TEXTS]
    matched_texts = {it["text"] for it in kept}
    missing = KEEP_TEXTS - matched_texts
    if missing:
        print("WARNING: these KEEP_TEXTS did not match any item in the source template "
              "(check for exact wording/typos) — they will be SKIPPED:")
        for m in sorted(missing):
            print(f"  - {m!r}")
        proceed = input("Continue anyway and create the Battery template without them? [y/N] ")
        if proceed.strip().lower() != "y":
            print("Aborted — no template created.")
            return

    new_items = []
    for i, it in enumerate(kept, start=1):
        new_items.append({
            "position": i,
            "sno": it.get("sno", ""),
            "is_section": False,
            "text": it["text"],
            "reference": it.get("reference", ""),
            "prompt": it.get("prompt", ""),
            "active": 1,
        })

    new_id = tdb.create_template(
        NEW_TEMPLATE_NAME,
        blob=b"",
        items=new_items,
        note_text=source.get("note_text", "") if isinstance(source, dict) else "",
        header_labels={
            "customer_label": source.get("customer_label"),
            "address_label": source.get("address_label"),
            "job_label": source.get("job_label"),
        },
        kind=SOURCE_KIND,
        title=source.get("title", "") or "",
        has_header_row=bool(source.get("has_header_row", 1)),
    )

    print(f"Created template {NEW_TEMPLATE_NAME!r} (id={new_id}, kind={SOURCE_KIND}) "
          f"with {len(new_items)} items copied from {SOURCE_TEMPLATE_NAME!r}.")
    for it in new_items:
        print(f"  [{it['position']:>2}] {it['text']}  (ref={it['reference'] or '-'})")


if __name__ == "__main__":
    main()
