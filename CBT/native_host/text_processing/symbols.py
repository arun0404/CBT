"""
Common symbols used in engineering documents.

These are expanded before sending text to Piper.
"""

SYMBOLS = {

    ">=": " greater than or equal to ",
    "<=": " less than or equal to ",
    "==": " equals to",
    "!=": " not equal to ",
    "=>": " implies ",
    "<>": " not equal to ",
    
    # Basic Operators
    "&": " and ",
    "@": " at ",
    "#": " number ",
    "%": " percent ",
    "+": " plus ",
    # "-": " minus ",
    "=": " equals ",
    "*": " times ",
    "×": " times ",
    "÷": " divided by ",
    "/": " slash ",
    "\\": " backslash ",

    # Comparison
    "<": " less than ",
    ">": " greater than ",
    "≤": " less than or equal to ",
    "≥": " greater than or equal to ",
    "≠": " not equal to ",
    "≈": " approximately equal to ",
    "≃": " approximately equal to ",

    # Mathematical
    "±": " plus or minus ",
    "∞": " infinity ",
    "√": " square root ",
    "∛": " cube root ",
    "∑": " summation ",
    "∏": " product ",
    "∆": " delta ",
    "∂": " partial ",
    "∫": " integral ",
    "∝": " proportional to ",
    "∴": " therefore ",
    "∵": " because ",

    # Degree
    "°": " degrees ",
    "℃": " degrees Celsius ",
    "°C": " degrees Celsius ",
    "℉": " degrees Fahrenheit ",
    "°F": " degrees Fahrenheit ",

    # Currency
    "₹": " rupees ",
    "$": " dollars ",
    "€": " euros ",
    "£": " pounds ",
    "¥": " yen ",

    # Arrows
    "→": " leads to ",
    "←": " comes from ",
    "↑": " up ",
    "↓": " down ",
    "↔": " bidirectional ",

    # Copyright
    "©": " copyright ",
    "®": " registered trademark ",
    "™": " trademark ",

    # Quotes. Double quotation marks are never spoken, so they are removed
    # (straight, then the typographic ones: left/right, low-9, high-reversed-9).
    # A lone one -- an isolated '"' or '“' word -- leaves nothing behind, so it
    # stays a silent, zero-width word for the highlighter.
    "\"": "",
    "“": "",
    "”": "",
    "„": "",
    "‟": "",
    "'": "'",

    # Brackets
    # "(": " open bracket ",
    # ")": " close bracket ",
    # "[": " open square bracket ",
    # "]": " close square bracket ",
    # "{": " open curly bracket ",
    # "}": " close curly bracket ",

    # Punctuation
    # ";": " semicolon ",
    # ":": " colon ",

    # Ellipsis
    # "...": " dot dot dot "
}