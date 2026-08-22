"""Safe, bounded metadata for browser captures persisted in jobs and traces."""

from __future__ import annotations

from typing import Any

from rental_hunt.bounds import BOUNDS


def browser_capture_metadata(capture: dict[str, Any]) -> dict[str, object]:
    """Describe a validated capture without retaining page text, HTML, or JSON bodies."""
    raw_pages = capture.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else []
    snapshots: list[dict[str, object]] = []
    for page_index, raw_page in enumerate(pages[: BOUNDS.pages_per_scan_max], start=1):
        if not isinstance(raw_page, dict):
            continue
        raw_documents = raw_page.get("json_documents")
        documents = raw_documents if isinstance(raw_documents, list) else []
        raw_candidates = raw_page.get("dom_candidates")
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        body_text = raw_page.get("body_text")
        document_html_prefix = raw_page.get("document_html_prefix")
        url = raw_page.get("url")
        snapshots.append(
            {
                "body_chars": len(body_text) if isinstance(body_text, str) else None,
                "captured_dom_candidates": len(candidates),
                "document_html_chars": (
                    len(document_html_prefix) if isinstance(document_html_prefix, str) else None
                ),
                "dom_candidate_count": _safe_non_negative_int(raw_page.get("dom_candidate_count")),
                "json_document_count": len(documents),
                "json_document_chars": sum(
                    len(document) for document in documents if isinstance(document, str)
                ),
                "page": page_index,
                "url": url if isinstance(url, str) else None,
            }
        )
    return {
        "capture_id": capture.get("capture_id"),
        "captured_pages": len(pages),
        "snapshots": snapshots,
    }


def _safe_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
