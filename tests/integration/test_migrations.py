"""Alembic upgrade builds the same tables the app expects on an empty database."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_upgrade_creates_schema(tmp_path: Path) -> None:
    database = tmp_path / "migrated.db"
    url = "sqlite+aiosqlite:///" + database.resolve().as_posix()
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    subprocess.run(
        [sys.executable, "-m", "catalogguard.migrate"],
        cwd=_ROOT,
        env=env,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "catalogguard.migrate"],
        cwd=_ROOT,
        env=env,
        check=True,
    )
    connection = sqlite3.connect(database)
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"store_listings", "llm_usage", "event_outbox", "alembic_version"} <= tables
    columns = {row[1] for row in connection.execute("PRAGMA table_info(event_outbox)")}
    assert {"claimed_at", "claim_token", "published_at"} <= columns

    assert {"email_suppressions", "email_handoffs"} <= tables
    suppression_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(email_suppressions)")
    }
    assert suppression_columns == {"customer_id", "reason", "created_at"}
    handoff_columns = {row[1] for row in connection.execute("PRAGMA table_info(email_handoffs)")}
    assert handoff_columns == {
        "id",
        "email_draft_id",
        "status",
        "payload",
        "suppressed_customer_ids",
        "created_at",
    }
    indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(email_handoffs)")}
    assert indexes.get("ix_email_handoffs_status") == 0
    assert any(
        unique
        and [col[2] for col in connection.execute(f"PRAGMA index_info('{name}')")]
        == ["email_draft_id"]
        for name, unique in indexes.items()
    )
    foreign_keys = list(connection.execute("PRAGMA foreign_key_list(email_handoffs)"))
    assert [(row[2], row[3], row[4]) for row in foreign_keys] == [
        ("email_drafts", "email_draft_id", "id")
    ]
    assert "dead_letter_redrives" in tables
    redrive_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(dead_letter_redrives)")
    }
    assert redrive_columns == {"id", "dead_letter_id", "status", "error", "created_at"}
    redrive_indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(dead_letter_redrives)")
    }
    assert "ix_dead_letter_redrives_dead_letter_id" in redrive_indexes
    redrive_keys = list(connection.execute("PRAGMA foreign_key_list(dead_letter_redrives)"))
    assert [(row[2], row[3], row[4]) for row in redrive_keys] == [
        ("dead_letters", "dead_letter_id", "id")
    ]
    assert "store_listing_revisions" in tables
    revision_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(store_listing_revisions)")
    }
    assert revision_columns == {
        "id",
        "sku",
        "store_id",
        "version",
        "published_draft_id",
        "title",
        "short_description",
        "long_description",
        "meta_title",
        "meta_description",
        "published_at",
    }
    revision_indexes = {
        row[1]: row[2] for row in connection.execute("PRAGMA index_list(store_listing_revisions)")
    }
    assert revision_indexes.get("ix_store_listing_revisions_published") == 0
    assert any(
        unique
        and [col[2] for col in connection.execute(f"PRAGMA index_info('{name}')")]
        == ["sku", "store_id", "version"]
        for name, unique in revision_indexes.items()
    )
    revision_keys = list(connection.execute("PRAGMA foreign_key_list(store_listing_revisions)"))
    assert [(row[2], row[3], row[4]) for row in revision_keys] == [
        ("listing_drafts", "published_draft_id", "id")
    ]
    version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("5751f906cab6",)
    connection.close()

    scripts = ScriptDirectory(str(_ROOT / "migrations"))
    assert scripts.get_current_head() == "5751f906cab6"
    assert scripts.get_revision("5751f906cab6").down_revision == "c81f0f63dcdc"
    assert scripts.get_revision("c81f0f63dcdc").down_revision == "15e685dc71f6"
