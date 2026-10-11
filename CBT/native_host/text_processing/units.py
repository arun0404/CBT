"""
Engineering Units Dictionary

These units are expanded before sending text to Piper.
Keys are matched case-insensitively.
"""

UNITS = {

    # ===============================
    # Length
    # ===============================

    "mm": "millimetres",
    "cm": "centimetres",
    "dm": "decimetres",
    "m": "metres",
    "km": "kilometres",

    "µm": "micrometres",
    "um": "micrometres",
    "nm": "nanometres",

    "in": "inches",
    "inch": "inch",
    "ft": "feet",
    "yd": "yards",
    "mi": "miles",

    # ===============================
    # Area
    # ===============================

    "mm²": "square millimetres",
    "cm²": "square centimetres",
    "m²": "square metres",
    "km²": "square kilometres",

    "sqmm": "square millimetres",
    "sqm": "square metres",

    # ===============================
    # Volume
    # ===============================

    "mm³": "cubic millimetres",
    "cm³": "cubic centimetres",
    "m³": "cubic metres",

    "cc": "cubic centimetres",

    "L": "litres",
    "l": "litres",

    "mL": "millilitres",
    "ml": "millilitres",

    # ===============================
    # Weight
    # ===============================

    "mg": "milligrams",
    "g": "grams",
    "kg": "kilograms",
    "ton": "tons",
    "tons": "tons",

    # ===============================
    # Time
    # ===============================

    "ms": "milliseconds",
    "sec": "seconds",
    "s": "seconds",
    "min": "minutes",
    "hr": "hours",
    "hrs": "hours",

    # ===============================
    # Speed
    # ===============================

    "rpm": "revolutions per minute",
    "rps": "revolutions per second",

    "km/h": "kilometres per hour",
    "mph": "miles per hour",

    "m/s": "metres per second",
    "ft/min": "feet per minute",
    "ft/s": "feet per second",

    # ===============================
    # Rates: flow, density, pressure, angle, length
    #
    # A key with a "/" is a whole ratio, matched as one token whether it is
    # written "5 kg/min", "5kg/min", "5 kg / min" or bare ("Flow (kg/min)").
    # Litres per minute / hour ("L/min", "L/h") are handled in preprocess.py.
    # ===============================

    "kg/min": "kilograms per minute",
    "g/L": "grams per litre",
    "ml/min": "millilitres per minute",
    "gal/min": "gallons per minute",
    "psi/min": "pounds per square inch per minute",
    "deg/s": "degrees per second",
    "°/s": "degrees per second",
    "nm/s": "nanometres per second",

    # ===============================
    # Pressure
    # ===============================

    "Pa": "pascal",
    "kPa": "kilopascal",
    "MPa": "megapascal",
    "GPa": "gigapascal",

    "bar": "bar",

    "psi": "pounds per square inch",

    # ===============================
    # Temperature
    # ===============================

    "°C": "degrees Celsius",
    "℃": "degrees Celsius",

    "°F": "degrees Fahrenheit",
    "℉": "degrees Fahrenheit",

    "K": "Kelvin",

    # ===============================
    # Electrical
    # ===============================

    "V": "volts",
    "kV": "kilovolts",
    "mV": "millivolts",

    "A": "amperes",
    "mA": "milliamperes",

    "W": "watts",
    "kW": "kilowatts",
    "MW": "megawatts",

    "Wh": "watt hours",
    "kWh": "kilowatt hours",

    "Hz": "hertz",
    "kHz": "kilohertz",
    "MHz": "megahertz",
    "GHz": "gigahertz",

    "Ω": "ohms",
    "ohm": "ohms",

    # ===============================
    # Force
    # ===============================

    "N": "newtons",
    "kN": "kilonewtons",

    # ===============================
    # Torque
    # ===============================

    "Nm": "newton metres",

    # ===============================
    # Energy
    # ===============================

    "J": "joules",
    "kJ": "kilojoules",

    # ===============================
    # Frequency
    # ===============================

    "cps": "cycles per second",

    # ===============================
    # Angle
    # ===============================

    "deg": "degrees",

    "rad": "radians",

    # ===============================
    # Percentage
    # ===============================

    "%": "percent"

}