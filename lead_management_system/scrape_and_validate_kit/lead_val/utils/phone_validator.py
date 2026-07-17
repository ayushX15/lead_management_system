import os

import phonenumbers  # type: ignore
import phonenumbers.carrier as phone_carrier  # type: ignore
import pandas as pd  # type: ignore

def validate_phone(raw: str) -> dict:
    if pd.isna(raw) or raw is None or str(raw).strip() == "":
        return {'is_valid_format': False, 'formatted': None, 'number_type': 'MISSING', 'carrier': None}

    raw_str = str(raw).strip()

    try:
        # Region for numbers without an explicit +country prefix (PHONE_REGION env)
        region = os.getenv("PHONE_REGION", "IN").upper()
        parsed_number = phonenumbers.parse(raw_str, region)
        if phonenumbers.is_valid_number(parsed_number):
            # Map enum to string name if possible
            type_int = phonenumbers.number_type(parsed_number)
            
            # Create mapping for common types to human readable strings
            type_mapping = {
                phonenumbers.PhoneNumberType.FIXED_LINE: "FIXED_LINE",
                phonenumbers.PhoneNumberType.MOBILE: "MOBILE",
                phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "FIXED_LINE_OR_MOBILE",
                phonenumbers.PhoneNumberType.TOLL_FREE: "TOLL_FREE",
                phonenumbers.PhoneNumberType.PREMIUM_RATE: "PREMIUM_RATE",
                phonenumbers.PhoneNumberType.SHARED_COST: "SHARED_COST",
                phonenumbers.PhoneNumberType.VOIP: "VOIP",
                phonenumbers.PhoneNumberType.PERSONAL_NUMBER: "PERSONAL_NUMBER",
                phonenumbers.PhoneNumberType.PAGER: "PAGER",
                phonenumbers.PhoneNumberType.UAN: "UAN",
                phonenumbers.PhoneNumberType.VOICEMAIL: "VOICEMAIL",
                phonenumbers.PhoneNumberType.UNKNOWN: "UNKNOWN"
            }
            
            type_str = type_mapping.get(type_int, str(type_int))
            carrier_name = phone_carrier.name_for_number(parsed_number, "en") or "Unknown"
            
            return {
                'is_valid_format': True, 
                'formatted': phonenumbers.format_number(parsed_number, phonenumbers.PhoneNumberFormat.E164), 
                'number_type': type_str,
                'carrier': carrier_name
            }
        else:
            return {'is_valid_format': False, 'formatted': None, 'number_type': None, 'carrier': None}
    except Exception:
        return {'is_valid_format': False, 'formatted': None, 'number_type': None, 'carrier': None}

def validate_phone_batch(lead_row: dict, phone_columns: list) -> dict:
    results = {}
    for col in phone_columns:
        if col in lead_row:
            result = validate_phone(lead_row[col])
            results[f"{col}_valid"] = 'VALID' if result['is_valid_format'] else 'INVALID'
            results[f"{col}_carrier_name"] = result.get('carrier', 'Unknown') or 'Unknown'
            results[f"{col}_number_type"] = result.get('number_type', 'UNKNOWN') or 'UNKNOWN'
    return results
