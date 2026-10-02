"""
Engineering abbreviations used in technical documents.

Keys are matched case-insensitively.
"""

ENGINEERING_TERMS = {

    # Mechanical
    "OD": "Outer Diameter",
    "ID": "Inner Diameter",
    "Dia": "Diameter",
    "THK": "Thickness",
    "HT": "Height",
    "WD": "Width",
    "LEN": "Length",
    # NOTE: bare single-letter dimension keys ("L", "W", "H") are
    # deliberately NOT here. Matched case-insensitively and standalone
    # they hit far more than dimensions -- the "h" in "km/h", a lone
    # "l"/"w" mid-sentence, etc. -- and corrupted unit compounds before
    # replace_units could run. Dimension abbreviations are now expanded
    # only inside an explicit dimension pattern ("L x W x H", "H:") by
    # TextPreprocessor.expand_dimension_abbreviations().

    # Assembly
    "ASSY": "Assembly",
    "ASM": "Assembly",
    "SUB ASSY": "Sub Assembly",

    # Drawing
    "DWG": "Drawing",
    "REV": "Revision",
    "QTY": "Quantity",
    "ITEM": "Item",
    "REF": "Reference",
    "FIG": "Figure",

    # Manufacturing
    "STD": "Standard",
    "TOL": "Tolerance",
    "MAX": "Maximum",
    "MIN": "Minimum",
    "AVG": "Average",

    # Measurements
    "RPM": "Revolutions Per Minute",
    "PSI": "Pounds Per Square Inch",
    "MPA": "Mega Pascal",
    "BAR": "Bar",

    # Electrical
    "AC": "Alternating Current",
    "DC": "Direct Current",
    "VAC": "Volts AC",
    "VDC": "Volts DC",

    # Temperature
    "TEMP": "Temperature",

    # Common engineering
    "MATL": "Material",
    "SPEC": "Specification",
    "APPROX": "Approximately",

    # CAD
    "CAD": "Computer Aided Design",
    "CAE": "Computer Aided Engineering",
    "CAM": "Computer Aided Manufacturing",

    # Fasteners
    "UNC": "Unified National Coarse",
    "UNF": "Unified National Fine",

    # Bearings
    "BRG": "Bearing",

    # Automotive
    "VIN": "Vehicle Identification Number",

    # General
    "NO": "Number"

}