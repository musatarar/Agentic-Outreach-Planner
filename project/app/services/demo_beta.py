def score_lead(quotes_created: int, deals_closed: int) -> int:
    """Deterministic demo score: quotes + 3 * closed deals."""
    return quotes_created + 3 * deals_closed
