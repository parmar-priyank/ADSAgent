"""Fix garbled UTF-8 sequences in main.py that were introduced by a bad editor encoding."""
with open("main.py", "r", encoding="utf-8") as f:
    t = f.read()

# Each entry: (garbled sequence still in file, clean ASCII replacement)
# These are UTF-8 bytes mis-decoded as latin-1, then partially fixed by the
# earlier curly-quote replacement, leaving broken fragments.
replacements = [
    ("â", "--"),   # en-dash  U+2013
    ("â", "--"),   # em-dash  U+2014
    ("â", "'"),    # right single quote U+2019
    ("â", "'"),    # left single quote  U+2018
    ("â", "->"),   # right arrow U+2192
    ("â¢", "-"),    # bullet U+2022
    # catch any remaining â€ fragments (the â was left after " replacement)
    ("â", "--"),
    ("â¬", "EUR"),  # euro sign (unlikely but safe)
]

for old, new in replacements:
    t = t.replace(old, new)

with open("main.py", "w", encoding="utf-8") as f:
    f.write(t)

print("Done")
