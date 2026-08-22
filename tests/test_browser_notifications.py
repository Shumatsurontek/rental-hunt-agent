from __future__ import annotations

import uuid
from collections.abc import Callable

from rental_hunt.bounds import BOUNDS
from rental_hunt.browser_notifications import (
    format_bootstrap_notification,
    format_listing_notification,
)
from rental_hunt.contracts import ListingAssessment, NormalizedListing
from rental_hunt.repository import DigestEntry


def _entry(
    listing_factory: Callable[..., NormalizedListing],
    index: int,
    *,
    assessed: bool = True,
) -> DigestEntry:
    listing = listing_factory(
        source_listing_id=str(100_000_000 + index),
        canonical_url=f"https://www.seloger.com/annonces/{100_000_000 + index}",
        title=f"Listing {index} with a deliberately descriptive title",
    )
    assessment = (
        ListingAssessment(
            score=100 - index,
            confidence="high",
            summary="A concise fit summary.",
            strengths=("quiet",),
            risks=(),
            unknowns=(),
        )
        if assessed
        else None
    )
    return DigestEntry(listing_id=uuid.uuid4(), listing=listing, assessment=assessment)


def test_listing_notification_is_actionable_and_bounded(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    entry = _entry(listing_factory, 1)

    payload = format_listing_notification(entry)

    assert payload.listing_id == entry.listing_id
    assert payload.listing_url == entry.listing.canonical_url
    assert payload.score == 99
    assert len(payload.title) <= BOUNDS.browser_notification_title_chars_max
    assert len(payload.message) <= BOUNDS.browser_notification_message_chars_max


def test_bootstrap_notification_contains_bounded_top_ten_and_explicit_counts(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    entries = tuple(_entry(listing_factory, index) for index in range(12))
    entries += (_entry(listing_factory, 20, assessed=False),)

    payload = format_bootstrap_notification(entries, eligible_total=25)

    assert "25 eligible; 12 assessed; 13 unassessed" in payload.message
    assert "Top 10:" in payload.message
    assert "10." in payload.message
    assert "11." not in payload.message
    assert len(payload.message) <= BOUNDS.browser_notification_message_chars_max


def test_listing_notification_marks_missing_assessment(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    payload = format_listing_notification(_entry(listing_factory, 1, assessed=False))

    assert payload.score is None
    assert "Analysis unavailable after bounded retries" in payload.message
