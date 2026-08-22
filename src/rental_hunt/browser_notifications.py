"""Pure formatting for the Chrome extension's durable notification outbox."""

from __future__ import annotations

import re

from rental_hunt.bounds import BOUNDS
from rental_hunt.contracts import BrowserNotificationPayload
from rental_hunt.repository import DigestEntry


def format_listing_notification(entry: DigestEntry) -> BrowserNotificationPayload:
    listing = entry.listing
    assessment = entry.assessment
    score = assessment.score if assessment is not None else None
    title = "New hard match" + (f" · {score}/100" if score is not None else "")
    price = f"€{listing.rent_eur_monthly}/mo" if listing.rent_eur_monthly else "rent unknown"
    surface = f"{listing.surface_m2} m²" if listing.surface_m2 else "surface unknown"
    location = " ".join(value for value in (listing.postal_code, listing.city) if value)
    analysis = (
        assessment.summary
        if assessment is not None
        else "Analysis unavailable after bounded retries."
    )
    message = _bounded_text(
        " · ".join(
            value
            for value in (
                listing.title,
                price,
                surface,
                location,
                analysis,
            )
            if value
        ),
        BOUNDS.browser_notification_message_chars_max,
    )
    return BrowserNotificationPayload(
        title=title,
        message=message,
        listing_id=entry.listing_id,
        listing_url=listing.canonical_url,
        score=score,
    )


def format_bootstrap_notification(
    entries: tuple[DigestEntry, ...],
    *,
    eligible_total: int,
) -> BrowserNotificationPayload:
    ordered = sorted(
        entries,
        key=lambda entry: entry.assessment.score if entry.assessment is not None else -1,
        reverse=True,
    )
    displayed = ordered[: BOUNDS.bootstrap_display_max]
    labels = [
        f"{index}. "
        + (f"{entry.assessment.score}/100 " if entry.assessment is not None else "n/a ")
        + _bounded_text(entry.listing.title, 28)
        for index, entry in enumerate(displayed, start=1)
    ]
    assessed = sum(entry.assessment is not None for entry in entries)
    unassessed = max(eligible_total - assessed, 0)
    parts = [f"Baseline: {eligible_total} eligible; {assessed} assessed; {unassessed} unassessed."]
    if labels:
        parts.append(f"Top {len(labels)}: " + " | ".join(labels))
    if not labels:
        parts.append("No ranked match is available yet.")
    return BrowserNotificationPayload(
        title="Rental watch baseline ready",
        message=_bounded_text(" ".join(parts), BOUNDS.browser_notification_message_chars_max),
    )


def _bounded_text(value: str, limit: int) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
