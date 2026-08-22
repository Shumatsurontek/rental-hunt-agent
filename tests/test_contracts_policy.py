from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rental_hunt.bounds import BOUNDS
from rental_hunt.contracts import (
    ListingAssessment,
    NormalizedListing,
    PreferencesUpdate,
    ScanResult,
    listing_fingerprint,
)
from rental_hunt.policy import evaluate_listing


def _preferences(**overrides: object) -> PreferencesUpdate:
    values: dict[str, object] = {
        "rent_eur_monthly_max": 1_500,
        "surface_m2_min": Decimal("45.00"),
        "rooms_min": 2,
        "furnished": "required",
        "postal_codes_allowed": ("75011",),
        "soft_preferences": ("quiet",),
    }
    values.update(overrides)
    return PreferencesUpdate.model_validate(values)


def test_hard_constraint_boundaries_are_inclusive(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    listing = listing_factory(rent_eur_monthly=1_500, surface_m2=Decimal("45"), rooms=2)

    result = evaluate_listing(listing, _preferences())

    assert result.eligible
    assert result.violations == ()
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("overrides", "violation"),
    [
        ({"rent_eur_monthly": 1_501}, "monthly rent exceeds"),
        ({"surface_m2": Decimal("44.99")}, "surface area is below"),
        ({"rooms": 1}, "room count is below"),
        ({"furnished": False}, "listing is not furnished"),
        ({"postal_code": "75012"}, "postal code is outside"),
    ],
)
def test_known_hard_constraint_violations_are_rejected(
    listing_factory: Callable[..., NormalizedListing],
    overrides: dict[str, object],
    violation: str,
) -> None:
    result = evaluate_listing(listing_factory(**overrides), _preferences())

    assert not result.eligible
    assert any(violation in value for value in result.violations)


@pytest.mark.parametrize(
    ("field", "warning"),
    [
        ("rent_eur_monthly", "monthly rent is unknown"),
        ("surface_m2", "surface area is unknown"),
        ("rooms", "room count is unknown"),
        ("furnished", "furnished status is unknown"),
        ("postal_code", "postal code is unknown"),
    ],
)
def test_unknown_hard_constraint_data_stays_eligible(
    listing_factory: Callable[..., NormalizedListing],
    field: str,
    warning: str,
) -> None:
    result = evaluate_listing(listing_factory(**{field: None}), _preferences())

    assert result.eligible
    assert warning in result.warnings


def test_furnished_forbidden_and_any(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    listing = listing_factory(furnished=True)

    assert not evaluate_listing(listing, _preferences(furnished="forbidden")).eligible
    assert evaluate_listing(listing, _preferences(furnished="any")).eligible


def test_fingerprint_ignores_observation_and_photo_presentation(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    first = listing_factory()
    second_values = first.model_dump(mode="python")
    second_values["observed_at"] = first.observed_at.replace(hour=13)
    second_values["photo_urls"] = ("https://img.seloger.com/new.jpg",)

    assert listing_fingerprint(first) == listing_fingerprint(second_values)


def test_fingerprint_changes_for_semantic_price_change(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    first = listing_factory()
    changed = first.model_dump(mode="python")
    changed["rent_eur_monthly"] = 1_501

    assert listing_fingerprint(first) != listing_fingerprint(changed)


def test_contract_collection_bounds() -> None:
    with pytest.raises(ValidationError):
        PreferencesUpdate(
            rent_eur_monthly_max=2_000,
            surface_m2_min=40,
            postal_codes_allowed=tuple(f"75{index:03d}" for index in range(51)),
        )
    with pytest.raises(ValidationError):
        ListingAssessment(
            score=50,
            confidence="low",
            summary="test",
            strengths=("a", "b", "c", "d"),
            risks=(),
            unknowns=(),
        )


def test_scan_result_rejects_duplicate_and_excessive_results(
    listing_factory: Callable[..., NormalizedListing],
) -> None:
    listing = listing_factory()
    with pytest.raises(ValidationError, match="duplicate source listing IDs"):
        ScanResult(
            listings=(listing, listing),
            observed_at=listing.observed_at,
            source_total=2,
            pages_scanned=1,
        )

    # Field-level validation fails before the model invariant at the hard bound.
    with pytest.raises(ValidationError):
        ScanResult(
            listings=tuple(listing for _ in range(BOUNDS.listings_per_scan_max + 1)),
            observed_at=listing.observed_at,
            source_total=BOUNDS.listings_per_scan_max,
            pages_scanned=1,
        )


def test_bound_configuration_is_immutable() -> None:
    changed = replace(BOUNDS, job_queue_max=1)
    with pytest.raises(FrozenInstanceError):
        changed.job_queue_max = 2  # type: ignore[misc]
