import sqlite3

from app.db import Database
from app.security import hash_secret


def test_multiple_repositories_and_one_time_link_code(tmp_path) -> None:
    database = Database(str(tmp_path / "notifier.sqlite3"))
    database.initialize()

    database.add_repository(
        "repo-a", "Group/Project-A", "Project A", hash_secret("one"), -1001
    )
    database.add_repository(
        "repo-b", "Group/Project-B", "Project B", hash_secret("two"), None
    )

    assert [row["repository_key"] for row in database.list_repositories()] == [
        "repo-a",
        "repo-b",
    ]
    assert database.get_repository("repo-a")["project_path"] == "group/project-a"
    assert database.get_repository("repo-b")["shared_chat_id"] is None
    assert database.get_repository("repo-a")["shared_thread_id"] is None

    assert database.set_repository_topic("repo-a", -1001, 77)
    assert database.get_repository("repo-a")["shared_thread_id"] == 77
    assert database.set_repository_topic("repo-a", -1001, None)
    assert database.get_repository("repo-a")["shared_thread_id"] is None

    database.create_link_code("single-use-code", "@Alice", 60, "42")
    assert (
        database.redeem_link_code("single-use-code", 12345, 67890, "alice_in_telegram")
        == "alice"
    )
    assert (
        database.redeem_link_code(
            "single-use-code", 99999, 99999, "different_telegram_user"
        )
        is None
    )

    link = database.get_active_link("ALICE", "42")
    assert link["telegram_chat_id"] == 12345
    assert link["telegram_user_id"] == 67890


def test_existing_database_is_migrated_for_topics(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE repositories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository_key TEXT NOT NULL UNIQUE,
                project_path TEXT NOT NULL,
                display_name TEXT NOT NULL,
                secret_hash TEXT NOT NULL,
                shared_chat_id INTEGER,
                enabled_events TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )

    database = Database(str(path))
    database.initialize()

    with database.connect() as connection:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(repositories)")
        }
    assert "shared_thread_id" in columns
