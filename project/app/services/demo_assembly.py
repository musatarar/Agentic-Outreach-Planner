from project.app.services.demo_alpha import normalize_lead_name
from project.app.services.demo_beta import score_lead


def demo_summary(raw_name: str, quotes_created: int, deals_closed: int) -> dict:
    """Compose alpha + beta into the demo summary payload."""
    return {
        "name": normalize_lead_name(raw_name),
        "score": score_lead(quotes_created, deals_closed),
    }
