from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from html import escape

from app.db import Database
from app.telegram import TelegramError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Person:
    user_id: str | None
    username: str | None


@dataclass
class GitLabEvent:
    category: str
    event_id: str
    project_name: str
    project_path: str
    title: str
    action: str
    author_name: str
    author_username: str | None
    url: str
    preview: str = ""
    metadata: list[tuple[str, str]] = field(default_factory=list)
    personal_targets: dict[Person, str] = field(default_factory=dict)
    mention_text: str = ""


def _excerpt(value: object, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return f"{text[: limit - 3]}..."
    return text


def _person(value: object) -> Person | None:
    if not isinstance(value, dict):
        return None
    user_id = value.get("id") or value.get("user_id")
    username = value.get("username") or value.get("user_username")
    if user_id is None and not username:
        return None
    return Person(
        str(user_id) if user_id is not None else None,
        str(username).casefold() if username else None,
    )


def _people(values: object) -> set[Person]:
    if not isinstance(values, list):
        return set()
    return {person for value in values if (person := _person(value))}


def _changed_people(payload: dict, field_name: str, action: str) -> set[Person]:
    current = _people(payload.get(field_name))
    if action in {"open", "reopen", "create"}:
        return current
    change = (payload.get("changes") or {}).get(field_name) or {}
    if not isinstance(change, dict):
        return set()
    previous = _people(change.get("previous"))
    changed_current = _people(change.get("current"))
    if changed_current:
        current = changed_current
    return current - previous


def _project(payload: dict) -> tuple[str, str, str]:
    project = payload.get("project") or {}
    if not isinstance(project, dict):
        project = {}
    name = str(project.get("name") or "GitLab project")
    path = str(
        project.get("path_with_namespace")
        or payload.get("project_path_with_namespace")
        or ""
    )
    url = str(project.get("web_url") or project.get("homepage") or "")
    return name, path, url


def _author(payload: dict) -> tuple[str, str | None]:
    user = payload.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    name = str(
        user.get("name")
        or payload.get("user_name")
        or payload.get("user_username")
        or "GitLab"
    )
    username = user.get("username") or payload.get("user_username")
    return name, str(username).casefold() if username else None


def parse_gitlab_event(
    payload: dict, header_event: str | None, event_id: str
) -> GitLabEvent:
    category = str(payload.get("object_kind") or "").casefold()
    if category == "tag_push":
        category = "tag_push"
    elif category in {"build", "job"}:
        category = "job"
    supported = {
        "issue",
        "job",
        "merge_request",
        "note",
        "pipeline",
        "push",
        "release",
        "tag_push",
    }
    if category not in supported:
        header = str(header_event or "").removesuffix(" Hook").replace(" ", "_")
        category = header.casefold()

    project_name, project_path, project_url = _project(payload)
    author_name, author_username = _author(payload)
    attrs = payload.get("object_attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    action = str(attrs.get("action") or payload.get("event_name") or category)
    title = project_name
    url = project_url
    preview = ""
    metadata: list[tuple[str, str]] = []
    targets: dict[Person, str] = {}
    mention_text = ""

    if category == "merge_request":
        title = str(attrs.get("title") or "Merge request")
        url = str(attrs.get("url") or project_url)
        preview = _excerpt(attrs.get("description"))
        metadata.extend(
            [
                ("Состояние", str(attrs.get("state") or action)),
                (
                    "Ветка",
                    f"{attrs.get('source_branch', '?')} → "
                    f"{attrs.get('target_branch', '?')}",
                ),
            ]
        )
        for person in _changed_people(payload, "assignees", action):
            targets[person] = "Вас назначили исполнителем Merge Request"
        for person in _changed_people(payload, "reviewers", action):
            targets[person] = "Вас назначили ревьюером Merge Request"

    elif category == "issue":
        title = str(attrs.get("title") or "Issue")
        url = str(attrs.get("url") or project_url)
        preview = _excerpt(attrs.get("description"))
        metadata.append(("Состояние", str(attrs.get("state") or action)))
        due_date = attrs.get("due_date")
        if due_date:
            metadata.append(("Дедлайн", str(due_date)))
        for person in _changed_people(payload, "assignees", action):
            targets[person] = "Вас назначили исполнителем Issue"

    elif category == "note":
        noteable = str(attrs.get("noteable_type") or "объекте GitLab")
        title = str(
            (payload.get("merge_request") or {}).get("title")
            or (payload.get("issue") or {}).get("title")
            or f"Комментарий в {noteable}"
        )
        url = str(attrs.get("url") or project_url)
        mention_text = str(attrs.get("note") or "")
        preview = _excerpt(mention_text)
        metadata.append(("Тип", noteable))

    elif category == "pipeline":
        title = f"Pipeline #{attrs.get('id', '')}".strip()
        status = str(attrs.get("status") or "unknown")
        metadata.extend(
            [
                ("Статус", status),
                ("Ветка", str(attrs.get("ref") or "—")),
            ]
        )
        url = str(attrs.get("url") or project_url)
        action = status

    elif category == "job":
        title = str(payload.get("build_name") or "Job")
        status = str(payload.get("build_status") or "unknown")
        metadata.extend(
            [
                ("Статус", status),
                ("Этап", str(payload.get("build_stage") or "—")),
                ("Ветка", str(payload.get("ref") or "—")),
            ]
        )
        url = str(payload.get("build_url") or project_url)
        action = status

    elif category in {"push", "tag_push"}:
        ref = str(payload.get("ref") or "").removeprefix("refs/heads/")
        ref = ref.removeprefix("refs/tags/")
        commits = payload.get("commits") or []
        title = f"Push в {ref}" if category == "push" else f"Изменение тега {ref}"
        metadata.append(("Коммитов", str(payload.get("total_commits_count") or 0)))
        if commits and isinstance(commits[0], dict):
            preview = _excerpt(commits[0].get("message"))
            url = str(commits[0].get("url") or project_url)

    elif category == "release":
        title = str(payload.get("name") or payload.get("tag") or "Release")
        url = str(payload.get("url") or project_url)
        preview = _excerpt(payload.get("description"))
        if payload.get("tag"):
            metadata.append(("Тег", str(payload["tag"])))

    return GitLabEvent(
        category=category,
        event_id=event_id,
        project_name=project_name,
        project_path=project_path,
        title=title,
        action=action,
        author_name=author_name,
        author_username=author_username,
        url=url,
        preview=preview,
        metadata=metadata,
        personal_targets=targets,
        mention_text=mention_text,
    )


CATEGORY_NAMES = {
    "issue": "Issue",
    "job": "Job",
    "merge_request": "Merge Request",
    "note": "Комментарий",
    "pipeline": "Pipeline",
    "push": "Push",
    "release": "Release",
    "tag_push": "Tag",
}


def format_message(event: GitLabEvent, heading: str) -> tuple[str, dict | None]:
    lines = [f"<b>{escape(heading)}</b>", "", f"<b>{escape(event.title)}</b>"]
    lines.append(f"Проект: {escape(event.project_name)}")
    if event.author_name:
        lines.append(f"Автор: {escape(event.author_name)}")
    for label, value in event.metadata:
        if value:
            lines.append(f"{escape(label)}: {escape(value)}")
    if event.preview:
        lines.extend(["", "<b>Превью:</b>", escape(event.preview)])
    markup = None
    if event.url.startswith(("https://", "http://")):
        markup = {"inline_keyboard": [[{"text": "Открыть в GitLab", "url": event.url}]]}
    return "\n".join(lines), markup


class GitLabNotifier:
    def __init__(self, database: Database, telegram):
        self.database = database
        self.telegram = telegram

    def _mention_targets(self, event: GitLabEvent) -> dict[Person, str]:
        targets: dict[Person, str] = {}
        if event.category != "note" or not event.mention_text:
            return targets
        text = event.mention_text.casefold()
        for link in self.database.list_active_links():
            username = str(link["gitlab_username"])
            pattern = rf"(?<![\w@])@{re.escape(username)}(?![\w-])"
            if re.search(pattern, text):
                targets[
                    Person(
                        str(link["gitlab_user_id"]) if link["gitlab_user_id"] else None,
                        username,
                    )
                ] = "Вас упомянули в комментарии GitLab"
        return targets

    async def _deliver(
        self,
        event: GitLabEvent,
        chat_id: int,
        target_key: str,
        heading: str,
        message_thread_id: int | None = None,
    ) -> bool:
        if not self.database.claim_delivery(event.event_id, target_key):
            logger.info(
                "Skipping duplicate GitLab notification event_id=%s target=%s",
                event.event_id,
                target_key,
            )
            return False
        message, markup = format_message(event, heading)
        try:
            await self.telegram.send_message(
                chat_id,
                message,
                reply_markup=markup,
                message_thread_id=message_thread_id,
            )
        except TelegramError:
            self.database.release_delivery(event.event_id, target_key)
            logger.exception(
                "Telegram delivery failed event_id=%s target=%s",
                event.event_id,
                target_key,
            )
            return False
        self.database.mark_delivery_sent(event.event_id, target_key)
        return True

    async def process(
        self,
        repository: sqlite3.Row,
        payload: dict,
        header_event: str | None,
        event_id: str,
    ) -> None:
        event = parse_gitlab_event(payload, header_event, event_id)
        expected_path = Database.normalize_project_path(repository["project_path"])
        actual_path = Database.normalize_project_path(event.project_path)
        if actual_path and actual_path != expected_path:
            logger.warning(
                "Ignoring GitLab project mismatch repository_key=%s expected=%s "
                "actual=%s",
                repository["repository_key"],
                expected_path,
                actual_path,
            )
            return
        enabled = self.database.repository_events(repository)
        if event.category not in enabled:
            logger.info(
                "Ignoring disabled GitLab event category=%s repository=%s",
                event.category,
                expected_path,
            )
            return

        sent = 0
        if repository["shared_chat_id"] is not None:
            heading = f"GitLab · {CATEGORY_NAMES.get(event.category, event.category)}"
            if await self._deliver(
                event,
                int(repository["shared_chat_id"]),
                f"shared:{repository['id']}:{repository['shared_chat_id']}",
                heading,
                repository["shared_thread_id"],
            ):
                sent += 1

        targets = {**event.personal_targets, **self._mention_targets(event)}
        for person, reason in targets.items():
            if event.author_username and person.username == event.author_username:
                continue
            link = self.database.get_active_link(person.username, person.user_id)
            if not link:
                logger.warning(
                    "No Telegram link for GitLab recipient username=%s user_id=%s",
                    person.username or "<missing>",
                    person.user_id or "<missing>",
                )
                continue
            if await self._deliver(
                event,
                int(link["telegram_chat_id"]),
                f"personal:{link['gitlab_username']}",
                reason,
            ):
                sent += 1

        logger.info(
            "GitLab webhook processed event_id=%s category=%s repository=%s sent=%d",
            event.event_id,
            event.category,
            expected_path,
            sent,
        )


def repository_summary(repository: sqlite3.Row) -> str:
    events = ",".join(sorted(json.loads(repository["enabled_events"])))
    state = "active" if repository["active"] else "disabled"
    chat = repository["shared_chat_id"] or "personal-only"
    thread = repository["shared_thread_id"] or "general"
    return (
        f"<b>{escape(repository['display_name'])}</b>\n"
        f"Path: <code>{escape(repository['project_path'])}</code>\n"
        f"Key: <code>{escape(repository['repository_key'])}</code>\n"
        f"Chat: <code>{chat}</code> · topic: <code>{thread}</code> · {state}\n"
        f"Events: <code>{escape(events)}</code>"
    )
