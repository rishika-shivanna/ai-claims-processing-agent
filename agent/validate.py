"""
Tool: field_validator
Pure, deterministic rule checks -- no LLM involved. These are the checks
listed explicitly in the spec (VIN format, valid date, numeric amounts).
Kept separate from extraction so extraction confidence and validation
correctness are two independently inspectable signals.
"""
from __future__ import annotations
import re
from datetime import datetime


def validate_vin(vin: str | None) -> tuple[bool, str | None]:
    if not vin:
        return False, "VIN is missing."
    if len(vin) != 17:
        return False, f"VIN is {len(vin)} characters, expected exactly 17."
    if not re.fullmatch(r"[A-Z0-9]{17}", vin):
        return False, "VIN contains characters outside A-Z0-9."
    return True, None


def validate_date(date_str: str | None) -> tuple[bool, str | None]:
    if not date_str:
        return False, "Date is missing."
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.year < 1990 or dt > datetime.now().replace(year=datetime.now().year + 1):
                return False, f"Date {date_str} is out of plausible range."
            return True, None
        except ValueError:
            continue
    return False, f"'{date_str}' is not a recognizable date."


def validate_numeric(value: str | None, field_label: str = "value") -> tuple[bool, str | None]:
    if not value:
        return False, f"{field_label} is missing."
    try:
        float(value)
        return True, None
    except ValueError:
        return False, f"'{value}' is not numeric."
