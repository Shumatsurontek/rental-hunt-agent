"""Named hard limits for every externally driven loop and payload."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Bounds:
    active_watches_max: int = 1
    active_listings_query_max: int = 1_000
    agent_file_calls_max: int = 6
    agent_model_calls_max: int = 3
    agent_output_tokens_max: int = 800
    agent_recursion_limit: int = 12
    agent_timeout_s: int = 200
    assessment_attempts_max: int = 2
    assessment_concurrency_max: int = 2
    bootstrap_assessments_max: int = 25
    bootstrap_display_max: int = 10
    body_text_chars_max: int = 500_000
    browser_capture_chars_max: int = 10_000_000
    browser_document_html_prefix_chars_max: int = 100_000
    browser_notification_message_chars_max: int = 500
    browser_notification_title_chars_max: int = 120
    browser_notifications_pending_max: int = 100
    browser_notifications_pull_max: int = 20
    chat_context_chars_max: int = 30_000
    chat_context_listings_max: int = 10
    chat_history_messages_max: int = 20
    chat_message_chars_max: int = 2_000
    chat_output_chars_max: int = 8_000
    chat_stream_chunks_max: int = 2_000
    chat_timeout_s: int = 60
    debug_artifacts_max: int = 20
    debug_retention_days: int = 7
    notification_prepare_concurrency_max: int = 2
    cleanup_rows_max: int = 1_000
    cleanup_tick_s: int = 3_600
    description_chars_max: int = 20_000
    job_claim_max: int = 20
    job_lease_s: int = 300
    job_queue_max: int = 500
    json_nodes_max: int = 50_000
    json_documents_bytes_max: int = 5_000_000
    json_scripts_max: int = 100
    json_script_bytes_max: int = 2_000_000
    listing_versions_max: int = 20
    listings_per_scan_max: int = 150
    navigation_timeout_s: int = 30
    pages_per_scan_max: int = 3
    photo_urls_max: int = 10
    poll_interval_s_default: int = 600
    poll_interval_s_max: int = 3_600
    poll_interval_s_min: int = 120
    watch_configuration_versions_max: int = 100
    scan_concurrency_max: int = 1
    scan_history_days: int = 30
    scan_timeout_s: int = 90
    source_attempts_max: int = 2
    notification_prepare_attempts_max: int = 5
    trace_flush_timeout_s: int = 5
    worker_tick_s: int = 2

    @property
    def langgraph_recursion_limit(self) -> int:
        # LangGraph counts the initial graph entry as step zero and raises before
        # executing a step equal to the configured limit. Add one so the public
        # bound remains 12 executable recursion steps.
        return self.agent_recursion_limit + 1


BOUNDS = Bounds()
