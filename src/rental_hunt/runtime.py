"""Owns process-lifetime resources and the three bounded background loops."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack

from langgraph.store.postgres import AsyncPostgresStore, PoolConfig
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from rental_hunt.agent import AgentService, build_chat_model
from rental_hunt.chat import ChatService
from rental_hunt.config import Settings
from rental_hunt.database import create_engine, create_session_factory
from rental_hunt.seloger import DebugArtifactStore, SeLogerSource
from rental_hunt.services import (
    JobWorker,
    cleanup_loop,
    scheduler_loop,
    worker_loop,
)
from rental_hunt.tracing import TraceManager

logger = logging.getLogger(__name__)


class ServiceRuntime:
    def __init__(  # noqa: PLR0913 - runtime owns a fixed set of process resources.
        self,
        *,
        settings: Settings,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        store: AsyncPostgresStore,
        stack: AsyncExitStack,
        agent: AgentService,
        chat: ChatService,
        tracing: TraceManager,
        worker: JobWorker,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.session_factory = session_factory
        self.store = store
        self.stack = stack
        self.agent = agent
        self.chat = chat
        self.tracing = tracing
        self.worker = worker
        self.stop_event = asyncio.Event()
        self.tasks: tuple[asyncio.Task[None], ...] = ()

    @classmethod
    async def create(cls, settings: Settings) -> ServiceRuntime:
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        stack = AsyncExitStack()
        try:
            store_context = AsyncPostgresStore.from_conn_string(
                settings.postgres_store_url,
                pool_config=PoolConfig(min_size=1, max_size=5),
            )
            store = await stack.enter_async_context(store_context)
            await store.setup()
            tracing = TraceManager(settings)
            stack.push_async_callback(tracing.close)
            model = await asyncio.to_thread(build_chat_model, settings)
            agent = AgentService(settings=settings, store=store, model=model, tracing=tracing)
            await agent.seed_skill()
            chat = ChatService(
                model=model,
                model_provider=settings.model_provider,
                model_name=settings.model_name,
                tracing=tracing,
            )
            source = SeLogerSource(
                user_data_dir=settings.playwright_user_data_dir,
                artifact_store=DebugArtifactStore(settings.debug_artifact_dir),
                headless=settings.playwright_headless,
            )
            worker = JobWorker(
                session_factory=session_factory,
                source=source,
                agent=agent,
                model_provider=settings.model_provider,
                model_name=settings.model_name,
                tracing=tracing,
            )
        except Exception:
            await stack.aclose()
            await engine.dispose()
            raise
        return cls(
            settings=settings,
            engine=engine,
            session_factory=session_factory,
            store=store,
            stack=stack,
            agent=agent,
            chat=chat,
            tracing=tracing,
            worker=worker,
        )

    async def start(self) -> None:
        if self.tasks:
            raise AssertionError("service runtime cannot be started twice")
        tasks = [
            asyncio.create_task(worker_loop(self.stop_event, self.worker), name="job-worker"),
            asyncio.create_task(
                cleanup_loop(self.stop_event, self.session_factory),
                name="history-cleanup",
            ),
        ]
        if self.settings.source_mode == "playwright":
            tasks.insert(
                0,
                asyncio.create_task(
                    scheduler_loop(self.stop_event, self.session_factory),
                    name="watch-scheduler",
                ),
            )
        self.tasks = tuple(tasks)

    def failed_task_names(self) -> tuple[str, ...]:
        return tuple(task.get_name() for task in self.tasks if task.done())

    async def close(self) -> None:
        self.stop_event.set()
        if self.tasks:
            try:
                async with asyncio.timeout(30):
                    await asyncio.gather(*self.tasks)
            except TimeoutError:
                logger.error("runtime_shutdown_timeout")
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.stack.aclose()
        await self.engine.dispose()
