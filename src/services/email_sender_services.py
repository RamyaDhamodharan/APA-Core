"""Small sender policy helpers shared by intake and notification paths."""


def is_automated_sender(sender: str | None) -> bool:
    """Identify sender addresses that must never receive automatic mail."""
    local = (sender or "").strip().casefold().partition("@")[0]
    return (
        local
        in {
            "no-reply",
            "noreply",
            "do-not-reply",
            "donotreply",
            "mailer-daemon",
            "postmaster",
        }
        or "mailer-daemon" in local
        or "noreply" in local
        or "no-reply" in local
    )


def may_send_automatic_reply(document) -> bool:
    """Block auto replies for bulk-tagged and automated intake messages."""
    if getattr(document, "source_flag", None) in {"bulk_sender", "auto_reply"}:
        return False
    return not is_automated_sender(getattr(document, "email_sender", None))
