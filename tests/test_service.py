import asyncio

from app.db import Database
from app.security import hash_secret
from app.service import GitLabNotifier, Person, parse_gitlab_event


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict | None]] = []

    async def send_message(
        self, chat_id: int, text: str, *, reply_markup: dict | None = None
    ) -> None:
        self.messages.append((chat_id, text, reply_markup))


def issue_payload() -> dict:
    return {
        "object_kind": "issue",
        "event_name": "issue",
        "user": {"name": "Maintainer", "username": "maintainer"},
        "project": {
            "name": "Example",
            "path_with_namespace": "group/example",
            "web_url": "https://gitlab.example/group/example",
        },
        "object_attributes": {
            "action": "open",
            "title": "Fix production",
            "description": "Detailed issue description",
            "state": "opened",
            "due_date": "2026-12-31",
            "url": "https://gitlab.example/group/example/-/issues/7",
        },
        "assignees": [{"id": 42, "username": "alice"}],
    }


def note_payload(note: str, author: str = "maintainer") -> dict:
    return {
        "object_kind": "note",
        "user": {"name": "Maintainer", "username": author},
        "project": {
            "name": "Example",
            "path_with_namespace": "group/example",
            "web_url": "https://gitlab.example/group/example",
        },
        "object_attributes": {
            "action": "create",
            "noteable_type": "Issue",
            "note": note,
            "url": "https://gitlab.example/group/example/-/issues/7#note_1",
        },
        "issue": {"title": "Fix production"},
    }


def linked_database(tmp_path) -> tuple[Database, object]:
    database = Database(str(tmp_path / "notifier.sqlite3"))
    database.initialize()
    repository = database.add_repository(
        "example-key",
        "group/example",
        "Example",
        hash_secret("secret"),
        -100100,
    )
    database.create_link_code("alice-code", "alice", 60, "42")
    database.redeem_link_code("alice-code", 1001, 2001, "alice_tg")
    return database, repository


def test_issue_parsing_resolves_assignee_and_details() -> None:
    event = parse_gitlab_event(issue_payload(), "Issue Hook", "event-1")

    assert event.category == "issue"
    assert event.project_path == "group/example"
    assert event.title == "Fix production"
    assert event.personal_targets == {
        Person("42", "alice"): "Вас назначили исполнителем Issue"
    }
    assert ("Дедлайн", "2026-12-31") in event.metadata
    assert event.preview == "Detailed issue description"


def test_shared_and_personal_notifications_are_sent_once(tmp_path) -> None:
    database, repository = linked_database(tmp_path)
    telegram = FakeTelegram()
    notifier = GitLabNotifier(database, telegram)

    asyncio.run(notifier.process(repository, issue_payload(), "Issue Hook", "event-1"))
    asyncio.run(notifier.process(repository, issue_payload(), "Issue Hook", "event-1"))

    assert [message[0] for message in telegram.messages] == [-100100, 1001]
    assert "GitLab · Issue" in telegram.messages[0][1]
    assert "Вас назначили исполнителем Issue" in telegram.messages[1][1]
    assert "Detailed issue description" in telegram.messages[1][1]


def test_comment_mention_sends_personal_notification(tmp_path) -> None:
    database, repository = linked_database(tmp_path)
    telegram = FakeTelegram()
    notifier = GitLabNotifier(database, telegram)

    asyncio.run(
        notifier.process(
            repository,
            note_payload("Please check this, @Alice."),
            "Note Hook",
            "event-2",
        )
    )

    assert [message[0] for message in telegram.messages] == [-100100, 1001]
    assert "Вас упомянули в комментарии GitLab" in telegram.messages[1][1]
    assert "Please check this, @Alice." in telegram.messages[1][1]


def test_self_mention_does_not_send_personal_notification(tmp_path) -> None:
    database, repository = linked_database(tmp_path)
    telegram = FakeTelegram()
    notifier = GitLabNotifier(database, telegram)

    asyncio.run(
        notifier.process(
            repository,
            note_payload("Reminder for @alice", author="alice"),
            "Note Hook",
            "event-3",
        )
    )

    assert [message[0] for message in telegram.messages] == [-100100]
