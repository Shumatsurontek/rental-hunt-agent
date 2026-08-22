"""Pure deterministic eligibility policy."""

from rental_hunt.contracts import Eligibility, NormalizedListing, PreferencesUpdate


def evaluate_listing(
    listing: NormalizedListing,
    preferences: PreferencesUpdate,
) -> Eligibility:
    violations: list[str] = []
    warnings: list[str] = list(listing.data_warnings)

    _check_rent(listing, preferences, violations, warnings)
    _check_surface(listing, preferences, violations, warnings)
    _check_rooms(listing, preferences, violations, warnings)
    _check_furnished(listing, preferences, violations, warnings)
    _check_postal_code(listing, preferences, violations, warnings)

    return Eligibility(
        eligible=not violations,
        violations=tuple(violations),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _check_rent(
    listing: NormalizedListing,
    preferences: PreferencesUpdate,
    violations: list[str],
    warnings: list[str],
) -> None:

    if listing.rent_eur_monthly is None:
        warnings.append("monthly rent is unknown")
    elif listing.rent_eur_monthly > preferences.rent_eur_monthly_max:
        violations.append("monthly rent exceeds the configured maximum")


def _check_surface(
    listing: NormalizedListing,
    preferences: PreferencesUpdate,
    violations: list[str],
    warnings: list[str],
) -> None:

    if listing.surface_m2 is None:
        warnings.append("surface area is unknown")
    elif listing.surface_m2 < preferences.surface_m2_min:
        violations.append("surface area is below the configured minimum")


def _check_rooms(
    listing: NormalizedListing,
    preferences: PreferencesUpdate,
    violations: list[str],
    warnings: list[str],
) -> None:

    if preferences.rooms_min is not None:
        if listing.rooms is None:
            warnings.append("room count is unknown")
        elif listing.rooms < preferences.rooms_min:
            violations.append("room count is below the configured minimum")


def _check_furnished(
    listing: NormalizedListing,
    preferences: PreferencesUpdate,
    violations: list[str],
    warnings: list[str],
) -> None:

    if preferences.furnished == "required":
        if listing.furnished is None:
            warnings.append("furnished status is unknown")
        elif listing.furnished is False:
            violations.append("listing is not furnished")
    elif preferences.furnished == "forbidden":
        if listing.furnished is None:
            warnings.append("furnished status is unknown")
        elif listing.furnished is True:
            violations.append("listing is furnished")


def _check_postal_code(
    listing: NormalizedListing,
    preferences: PreferencesUpdate,
    violations: list[str],
    warnings: list[str],
) -> None:

    if preferences.postal_codes_allowed:
        if listing.postal_code is None:
            warnings.append("postal code is unknown")
        elif listing.postal_code not in preferences.postal_codes_allowed:
            violations.append("postal code is outside the configured allowlist")
