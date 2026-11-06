from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager, suppress

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app.config import get_settings
from app.db import DEFAULT_EVENTS, Database
from app.security import event_fingerprint, hash_secret, verify_secret
from app.service import GitLabNotifier, repository_summary
from app.telegram import TelegramClient, TelegramError

settings = get_settings()
configured_log_level = settings.log_level.upper()
logging.basicConfig(level=configured_log_level)
logging.getLogger().setLevel(configured_log_level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

database = Database(settings.database_path)
telegram = TelegramClient(
    settings.telegram_bot_token, proxy_url=settings.telegram_proxy_url
)
notifier = GitLabNotifier(database, telegram)


async def _connect(message: dict, code: str) -> None:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = int(chat["id"])
    if chat.get("type") != "private":
        await telegram.send_message(
            chat_id, "Подключение личных уведомлений доступно только в личном чате."
        )
        return
    username = database.redeem_link_code(
        code,
        chat_id,
        int(sender["id"]),
        sender.get("username"),
    )
    if not username:
        await telegram.send_message(
            chat_id, "Код недействителен, уже использован или истёк."
        )
        return
    await telegram.send_message(
        chat_id,
        f"✅ GitLab <code>@{username}</code> подключён. Личные уведомления включены.",
    )


async def _admin_command(command: str, arguments: list[str], message: dict) -> bool:
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    chat_id = int(chat["id"])
    if command not in {
        "/addrepo",
        "/repos",
        "/repoevents",
        "/disablerepo",
        "/enablerepo",
        "/linkcode",
        "/settopic",
        "/cleartopic",
    }:
        return False
    if int(sender["id"]) not in settings.admin_ids:
        await telegram.send_message(chat_id, "Команда доступна только администратору.")
        return True

    if command == "/addrepo":
        if not arguments:
            await telegram.send_message(
                chat_id,
                "Использование: <code>/addrepo group/project "
                "[CHAT_ID|personal]</code>\n"
                "Без второго аргумента текущий чат станет общим чатом проекта.",
            )
            return True
        project_path = arguments[0].strip("/")
        shared_chat_id: int | None = chat_id
        shared_thread_id: int | None = message.get("message_thread_id")
        if len(arguments) > 1:
            if arguments[1].casefold() == "personal":
                shared_chat_id = None
                shared_thread_id = None
            else:
                try:
                    shared_chat_id = int(arguments[1])
                    shared_thread_id = None
                except ValueError:
                    await telegram.send_message(chat_id, "CHAT_ID должен быть числом.")
                    return True
        repository_key = secrets.token_urlsafe(9)
        webhook_secret = secrets.token_urlsafe(32)
        try:
            database.add_repository(
                repository_key,
                project_path,
                project_path,
                hash_secret(webhook_secret),
                shared_chat_id,
                shared_thread_id,
            )
        except Exception:
            logger.exception("Could not add GitLab repository path=%s", project_path)
            await telegram.send_message(chat_id, "Не удалось добавить репозиторий.")
            return True
        url = f"{settings.public_base_url}/webhooks/gitlab/{repository_key}"
        await telegram.send_message(
            chat_id,
            "✅ Репозиторий добавлен.\n\n"
            f"URL webhook:\n<code>{url}</code>\n\n"
            f"Secret token:\n<code>{webhook_secret}</code>\n\n"
            "Секрет показывается только сейчас. В GitLab включите нужные события "
            "и оставьте проверку SSL включённой.",
            message_thread_id=shared_thread_id,
        )
        return True

    if command == "/repos":
        repositories = database.list_repositories()
        if not repositories:
            await telegram.send_message(chat_id, "Репозитории ещё не добавлены.")
        else:
            await telegram.send_message(
                chat_id, "\n\n".join(repository_summary(row) for row in repositories)
            )
        return True

    if command == "/repoevents":
        if len(arguments) < 2:
            await telegram.send_message(
                chat_id,
                "Использование: <code>/repoevents KEY event1,event2</code>\n"
                f"Доступно: <code>{','.join(sorted(DEFAULT_EVENTS))}</code>",
            )
            return True
        events = {
            value.strip().casefold()
            for value in " ".join(arguments[1:]).split(",")
            if value.strip()
        }
        unknown = events - DEFAULT_EVENTS
        if not events or unknown:
            await telegram.send_message(
                chat_id,
                "Неизвестные события: "
                f"<code>{','.join(sorted(unknown)) or 'пустой список'}</code>",
            )
            return True
        if database.set_repository_events(arguments[0], events):
            await telegram.send_message(chat_id, "✅ Список событий обновлён.")
        else:
            await telegram.send_message(chat_id, "Репозиторий не найден.")
        return True

    if command in {"/disablerepo", "/enablerepo"}:
        if not arguments:
            await telegram.send_message(chat_id, f"Использование: {command} KEY")
            return True
        active = command == "/enablerepo"
        if database.set_repository_active(arguments[0], active):
            await telegram.send_message(
                chat_id,
                "✅ Репозиторий включён." if active else "✅ Репозиторий отключён.",
            )
        else:
            await telegram.send_message(chat_id, "Репозиторий не найден.")
        return True

    if command in {"/settopic", "/cleartopic"}:
        if not arguments:
            await telegram.send_message(chat_id, f"Использование: {command} KEY")
            return True
        current_thread_id = message.get("message_thread_id")
        if command == "/settopic" and current_thread_id is None:
            await telegram.send_message(
                chat_id,
                "Отправьте эту команду внутри нужного топика Telegram.",
            )
            return True
        target_thread_id = int(current_thread_id) if command == "/settopic" else None
        if database.set_repository_topic(arguments[0], chat_id, target_thread_id):
            answer = (
                "✅ Уведомления проекта направлены в этот топик."
                if command == "/settopic"
                else "✅ Уведомления проекта направлены в общий раздел чата."
            )
            await telegram.send_message(
                chat_id,
                answer,
                message_thread_id=current_thread_id,
            )
        else:
            await telegram.send_message(
                chat_id,
                "Репозиторий не найден.",
                message_thread_id=current_thread_id,
            )
        return True

    if command == "/linkcode":
        if not arguments:
            await telegram.send_message(
                chat_id,
                "Использование: <code>/linkcode @gitlab_username [USER_ID]</code>",
            )
            return True
        code = secrets.token_urlsafe(24)
        gitlab_user_id = arguments[1] if len(arguments) > 1 else None
        database.create_link_code(
            code,
            arguments[0],
            settings.link_code_ttl_minutes,
            gitlab_user_id,
        )
        if settings.telegram_bot_username:
            link = f"https://t.me/{settings.telegram_bot_username}?start={code}"
            answer = f"Одноразовая ссылка:\n{link}"
        else:
            answer = f"Одноразовый код: <code>{code}</code>"
        await telegram.send_message(chat_id, answer)
        return True

    return True


async def _handle_message(message: dict) -> None:
    text = str(message.get("text") or "").strip()
    if not text.startswith("/"):
        return
    parts = text.split()
    command = parts[0].split("@", 1)[0].casefold()
    arguments = parts[1:]
    chat = message.get("chat") or {}
    chat_id = int(chat["id"])

    if await _admin_command(command, arguments, message):
        return
    if command == "/start":
        if arguments:
            await _connect(message, arguments[0])
            return
        link = database.get_link_by_chat(chat_id)
        if link:
            database.set_link_active(chat_id, True)
            await telegram.send_message(chat_id, "✅ Личные уведомления включены.")
        else:
            await telegram.send_message(
                chat_id,
                "GitLab Telegram Notifier. Получите одноразовую ссылку "
                "у администратора.",
            )
        return
    if command == "/connect":
        if arguments:
            await _connect(message, arguments[0])
        else:
            await telegram.send_message(chat_id, "Использование: /connect КОД")
        return
    if command == "/status":
        link = database.get_link_by_chat(chat_id)
        if not link:
            await telegram.send_message(chat_id, "Аккаунт GitLab не подключён.")
        else:
            state = "включены" if link["active"] else "отключены"
            await telegram.send_message(
                chat_id,
                f"Уведомления: <b>{state}</b>\nGitLab: "
                f"<code>@{link['gitlab_username']}</code>",
            )
        return
    if command == "/stop":
        if database.set_link_active(chat_id, False):
            await telegram.send_message(chat_id, "Личные уведомления отключены.")
        else:
            await telegram.send_message(chat_id, "Аккаунт GitLab не подключён.")
        return
    if command == "/chatid":
        await telegram.send_message(chat_id, f"Chat ID: <code>{chat_id}</code>")
        return
    if command in {"/help", "/settings"}:
        await telegram.send_message(
            chat_id,
            "/status — состояние личных уведомлений\n"
            "/stop — отключить личные уведомления\n"
            "/start — включить их снова\n"
            "/chatid — показать ID текущего чата",
        )


async def _poll_telegram_updates() -> None:
    offset: int | None = None
    while True:
        try:
            updates = await telegram.get_updates(offset=offset, timeout=30)
            for update in updates:
                try:
                    if update.get("message"):
                        await _handle_message(update["message"])
                except Exception:
                    logger.exception(
                        "Failed to process Telegram update id=%s",
                        update.get("update_id"),
                    )
                finally:
                    if update.get("update_id") is not None:
                        offset = int(update["update_id"]) + 1
        except asyncio.CancelledError:
            raise
        except TelegramError as exc:
            logger.warning("Telegram polling failed: %s; retrying", exc)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    logger.info(
        "Notifier starting telegram_mode=%s proxy_configured=%s database=%s",
        settings.telegram_update_mode,
        bool(settings.telegram_proxy_url),
        settings.database_path,
    )
    polling_task: asyncio.Task | None = None
    if settings.telegram_update_mode == "webhook":
        assert settings.telegram_webhook_secret
        await telegram.set_webhook(
            settings.telegram_webhook_url, settings.telegram_webhook_secret
        )
    else:
        await telegram.delete_webhook()
        polling_task = asyncio.create_task(_poll_telegram_updates())
    yield
    if polling_task:
        polling_task.cancel()
        with suppress(asyncio.CancelledError):
            await polling_task
    await telegram.close()


app = FastAPI(
    title="GitLab Telegram Multi-Repo Notifier",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/gitlab/{repository_key}")
async def gitlab_webhook(
    repository_key: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
    x_gitlab_event_uuid: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
) -> dict:
    raw_body = await request.body()
    if len(raw_body) > settings.max_webhook_bytes:
        raise HTTPException(status_code=413, detail="Webhook payload too large")
    repository = database.get_repository(repository_key)
    if not repository or not repository["active"]:
        raise HTTPException(status_code=404, detail="Repository not found")
    if not verify_secret(x_gitlab_token, repository["secret_hash"]):
        logger.warning("Rejected GitLab webhook repository_key=%s", repository_key)
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    event_id = (
        x_gitlab_event_uuid
        or idempotency_key
        or event_fingerprint(repository_key, raw_body)
    )
    background_tasks.add_task(
        notifier.process,
        repository,
        payload,
        x_gitlab_event,
        event_id,
    )
    logger.info(
        "Accepted GitLab webhook event=%s event_id=%s repository=%s bytes=%d",
        x_gitlab_event or payload.get("object_kind") or "<missing>",
        event_id,
        repository["project_path"],
        len(raw_body),
    )
    return {"ok": True, "status": "accepted", "event_id": event_id}


@app.post("/webhooks/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if settings.telegram_update_mode != "webhook":
        raise HTTPException(status_code=404)
    if not secrets.compare_digest(
        x_telegram_bot_api_secret_token or "", settings.telegram_webhook_secret or ""
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram secret")
    update = await request.json()
    if update.get("message"):
        await _handle_message(update["message"])
    return {"ok": True}
