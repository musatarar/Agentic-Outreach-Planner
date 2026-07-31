def normalize_lead_name(raw: str) -> str:
    """Collapse whitespace runs and title-case an agency name."""
    return " ".join(raw.split()).title()
