---
name: listing-analysis
description: Rank and explain a normalized French rental listing against the owner's soft preferences after deterministic constraints have passed.
---

# Listing analysis

Read `/memories/preferences.md` before assessing the listing.

The deterministic service has already evaluated hard constraints. Never claim that your score
changes eligibility and never omit a listing because of the score.

Assess only evidence present in the normalized listing and preferences:

1. Summarize the fit in one concrete sentence.
2. Score the soft-preference fit from 0 to 100.
3. List at most three strengths, three risks, and three unknowns.
4. Treat missing data as unknown, not negative evidence.
5. Do not infer neighborhood quality, transit time, safety, landlord reliability, or facts that
   are absent from the listing.
6. Do not contact anyone, browse the web, write files, or delegate work.

Return only the requested structured assessment.
