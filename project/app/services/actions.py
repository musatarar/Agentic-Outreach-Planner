"""Action type constants for the outreach planner (per CONTRACT.md)."""

POWER_USER_REWARD = "power_user_reward"
FOLLOW_UP_AFTER_HOLD = "follow_up_after_hold"
REENGAGE_DORMANT = "reengage_dormant"
NUDGE_USAGE = "nudge_usage"
COMPLETE_ONBOARDING = "complete_onboarding"
UNKNOWN = "unknown"

ACTION_TYPES = [
    POWER_USER_REWARD,      # near milestone, offer volume pricing/discount (medium)
    FOLLOW_UP_AFTER_HOLD,   # asked to be contacted later; date passed (high)
    REENGAGE_DORMANT,       # onboarded but stopped using portal (high)
    NUDGE_USAGE,            # active but underusing, needs encouragement (medium)
    COMPLETE_ONBOARDING,    # demo done but never signed up; weight by book size (high)
    UNKNOWN,                # no pattern matched -> needs_human=True, report to BD
]

# Per-action metadata: human-readable label + default urgency.
ACTION_META = {
    POWER_USER_REWARD: {
        "label": "Reward power user (volume pricing)",
        "urgency": "medium",
    },
    FOLLOW_UP_AFTER_HOLD: {
        "label": "Follow up — hold period has passed",
        "urgency": "high",
    },
    REENGAGE_DORMANT: {
        "label": "Re-engage dormant account",
        "urgency": "high",
    },
    NUDGE_USAGE: {
        "label": "Nudge usage / encourage next step",
        "urgency": "medium",
    },
    COMPLETE_ONBOARDING: {
        "label": "Complete onboarding (demo done, never signed up)",
        "urgency": "high",
    },
    UNKNOWN: {
        "label": "Unknown — needs human review",
        "urgency": "low",
    },
}
