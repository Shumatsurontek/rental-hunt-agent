"""Operational commands for serving, migrations, and live dependency probes."""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import AsyncExitStack

import uvicorn
from langgraph.store.postgres import AsyncPostgresStore, PoolConfig

from rental_hunt.agent import AgentService, build_chat_model
from rental_hunt.api import create_app
from rental_hunt.config import Settings
from rental_hunt.database import run_migrations
from rental_hunt.logging import configure_logging
from rental_hunt.seloger import DebugArtifactStore, SeLogerSource
from rental_hunt.tracing import TraceManager


def main() -> None:
    parser = argparse.ArgumentParser(prog="rental-hunt")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve", help="Run migrations and start the API and workers.")
    subparsers.add_parser("db-upgrade", help="Apply domain database migrations.")
    subparsers.add_parser("agent-doctor", help="Probe model tool use and structured output.")
    source_parser = subparsers.add_parser("source-doctor", help="Run one bounded live scan.")
    source_parser.add_argument("--url", required=True)
    arguments = parser.parse_args()

    settings = Settings()
    configure_logging(settings.log_level)
    if arguments.command == "serve":
        run_migrations(settings.database_url)
        uvicorn.run(
            create_app(settings),
            host="0.0.0.0",  # noqa: S104 - Docker publishes only to loopback by default.
            port=8000,
            log_config=None,
            access_log=True,
            timeout_keep_alive=5,
        )
        return
    if arguments.command == "db-upgrade":
        run_migrations(settings.database_url)
        return
    if arguments.command == "agent-doctor":
        asyncio.run(_agent_doctor(settings))
        return
    if arguments.command == "source-doctor":
        asyncio.run(_source_doctor(settings, arguments.url))
        return
    raise AssertionError(f"unhandled command: {arguments.command}")


async def _agent_doctor(settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        tracing = TraceManager(settings)
        stack.push_async_callback(tracing.close)
        store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(
                settings.postgres_store_url,
                pool_config=PoolConfig(min_size=1, max_size=2),
            )
        )
        await store.setup()
        model = await asyncio.to_thread(build_chat_model, settings)
        agent = AgentService(settings=settings, store=store, model=model, tracing=tracing)
        await agent.seed_skill()
        assessment = await agent.doctor()
        print(assessment.model_dump_json(indent=2))


async def _source_doctor(settings: Settings, url: str) -> None:
    source = SeLogerSource(
        user_data_dir=settings.playwright_user_data_dir,
        artifact_store=DebugArtifactStore(settings.debug_artifact_dir),
        headless=settings.playwright_headless,
    )
    result = await source.scan(url)
    print(
        json.dumps(
            {
                "listing_count": len(result.listings),
                "pages_scanned": result.pages_scanned,
                "source_total": result.source_total,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
