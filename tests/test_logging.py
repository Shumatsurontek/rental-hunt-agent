from __future__ import annotations

import logging

from rental_hunt.logging import configure_logging


def test_http_client_request_urls_are_not_logged_at_info() -> None:
    configure_logging("INFO")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
