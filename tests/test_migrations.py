from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_alembic_revision_ids_fit_the_version_table() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    for revision in scripts.walk_revisions():
        assert len(revision.revision) <= 32, revision.revision
