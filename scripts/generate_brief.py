EXCLUDED_DATE_ISOS = {"2026-04-15","2026-04-17","2026-04-18"}


def get_available_dates():
    # other existing code...
    return [date for date in available_dates if date.isoformat() not in EXCLUDED_DATE_ISOS]


def load_all_days_data():
    # other existing code...
    for date in dates:
        if date.isoformat() in EXCLUDED_DATE_ISOS:
            continue
    # rest of the code...