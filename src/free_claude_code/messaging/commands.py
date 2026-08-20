import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

from .command_context import MessagingCommandContext
from .models import IncomingMessage
from .rendering.telegram_markdown import escape_md_v2



def _get_session_summary(session_id: str, workspace: str) -> tuple[str, str]:
    """Return (prompt_summary, modified_time_str) for a session_id."""
    if not session_id:
        return ("新對話", "")

    sanitized = workspace.strip("/").replace("/", "-")
    project_dir = Path.home() / ".claude" / "projects" / f"-{sanitized}"
    jsonl_file = project_dir / f"{session_id}.jsonl"

    if not jsonl_file.is_file():
        matches = list(
            (Path.home() / ".claude" / "projects").glob(f"**/{session_id}.jsonl")
        )
        if matches:
            jsonl_file = matches[0]
        else:
            return ("無詳細紀錄", "")

    mtime = jsonl_file.stat().st_mtime
    time_str = time.strftime("%m/%d %H:%M", time.localtime(mtime))

    summary = ""
    try:
        with open(jsonl_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                data = json.loads(line)
                msg = data.get("message", {})
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content")
                    text = ""
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                text = c.get("text", "")
                                break
                    text = text.strip()
                    if text.startswith("<local-command") or text.startswith(
                        "<command-message"
                    ):
                        continue
                    if text:
                        first_line = text.splitlines()[0].strip()
                        summary = first_line[:30] + (
                            "..." if len(first_line) > 30 else ""
                        )
                        break
    except Exception:
        pass

    if not summary:
        summary = "新對話"
    return (summary, time_str)



async def _send_stop_feedback(
    handler: MessagingCommandContext,
    incoming: IncomingMessage,
    suffix: str,
) -> None:
    """Send stop feedback only when no existing status can represent the result."""
    msg_id = await handler.outbound.queue_send_message(
        incoming.chat_id,
        handler.format_status("⏹", "已停止。", suffix),
        fire_and_forget=False,
        message_thread_id=incoming.message_thread_id,
    )
    handler.record_outgoing_message(
        incoming.platform, incoming.chat_id, msg_id, "command"
    )


async def handle_stop_command(
    handler: MessagingCommandContext, incoming: IncomingMessage
) -> None:
    """Handle /stop command from messaging platform."""
    if incoming.is_reply() and incoming.reply_to_message_id:
        outcome = await handler.stop_reply(
            incoming.scope,
            incoming.reply_to_message_id,
        )

        if outcome.cancelled_count == 0:
            await _send_stop_feedback(
                handler,
                incoming,
                "該筆訊息目前無正在運行的任務。",
            )
            return

        if outcome.requires_confirmation(incoming.scope):
            await _send_stop_feedback(
                handler,
                incoming,
                f"已成功取消 {outcome.cancelled_count} 筆請求。",
            )
        return

    outcome = await handler.stop_all_tasks()
    if outcome.cancelled_count == 0:
        await _send_stop_feedback(handler, incoming, "目前沒有正在運行的任務。")
    elif outcome.requires_confirmation(incoming.scope):
        await _send_stop_feedback(
            handler,
            incoming,
            f"已成功取消 {outcome.cancelled_count} 筆排隊或運行中的請求。",
        )


async def handle_stats_command(
    handler: MessagingCommandContext, incoming: IncomingMessage
) -> None:
    """Handle /stats command with Session ID lookup."""
    stats = handler.cli_manager.get_stats()
    tree_count = handler.get_tree_count()
    ctx = handler.get_render_ctx()

    session_ids: list[str] = []
    if hasattr(handler, "session_store") and handler.session_store:
        try:
            snapshot = handler.session_store.load_conversation_snapshot()
            for identity, tree in snapshot.trees.items():
                if (
                    identity.scope.platform == incoming.platform
                    and identity.scope.chat_id == incoming.chat_id
                ):
                    for node_data in tree.nodes.values():
                        if isinstance(node_data, dict):
                            sid = node_data.get("session_id")
                            if sid and sid not in session_ids:
                                session_ids.append(str(sid))
        except Exception as e:
            logger.debug("Failed to retrieve session IDs for stats: {}", e)

    lines = ["📊 " + ctx.bold("系統狀態")]
    if stats.get("active_sessions", 0) > 0:
        lines.append(ctx.escape_text(f"• ⚡ 運算中的任務: {stats['active_sessions']}"))
    lines.append(ctx.escape_text(f"• 對話樹分支: {tree_count}"))


    if session_ids:
        lines.extend(["", "🔑 " + ctx.bold("對話 Session 清單:")])
        workspace = (
            getattr(handler.cli_manager, "workspace", "")
            or os.getenv("ALLOWED_DIR", "")
        )
        for idx, sid in enumerate(reversed(session_ids)):
            summary, time_str = _get_session_summary(sid, workspace)
            label = " (最新)" if idx == 0 else ""
            time_info = f" ({time_str})" if time_str else ""
            header = f"• {summary}{time_info}"
            lines.append(ctx.bold(header) + ctx.escape_text(label))
            lines.append("  " + ctx.code_inline(sid))

        latest_sid = session_ids[-1]
        cmd_str = (
            f"cd {workspace} && claude -r {latest_sid}"
            if workspace
            else f"claude -r {latest_sid}"
        )

        lines.extend(
            [
                "",
                "💡 " + ctx.bold("在 Terminal 接續對話的指令："),
                ctx.code_inline(cmd_str),
            ]
        )


    msg_id = await handler.outbound.queue_send_message(
        incoming.chat_id,
        "\n".join(lines),
        fire_and_forget=False,
        message_thread_id=incoming.message_thread_id,
    )
    handler.record_outgoing_message(
        incoming.platform, incoming.chat_id, msg_id, "command"
    )


async def handle_fork_command(
    handler: MessagingCommandContext, incoming: IncomingMessage
) -> bool:
    """Handle /fork command."""
    parts = (incoming.text or "").strip().split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        return False

    ctx = handler.get_render_ctx()
    msg_text = (
        "🌿 "
        + ctx.bold("指令說明：/fork")
        + "\n\n"
        + ctx.escape_text("用法：/fork <提示詞>")
        + "\n"
        + ctx.escape_text("範例：/fork 嘗試使用另一種做法解題")
        + "\n\n"
        + ctx.escape_text(
            "說明：在提示詞前加上 /fork，系統會強制複製當前對話並開立全新的 Session ID 分支，保護原本的對話快照不被覆蓋。"
        )
    )
    msg_id = await handler.outbound.queue_send_message(
        incoming.chat_id,
        msg_text,
        fire_and_forget=False,
        message_thread_id=incoming.message_thread_id,
    )
    handler.record_outgoing_message(
        incoming.platform, incoming.chat_id, msg_id, "command"
    )
    return True





async def _delete_message_ids(
    handler: MessagingCommandContext, chat_id: str, msg_ids: set[str]
) -> None:
    """Best-effort delete messages by ID. Sorts numeric IDs descending."""
    if not msg_ids:
        return

    def _as_int(s: str) -> int | None:
        try:
            return int(str(s))
        except Exception:
            return None

    numeric: list[tuple[int, str]] = []
    non_numeric: list[str] = []
    for mid in msg_ids:
        n = _as_int(mid)
        if n is None:
            non_numeric.append(mid)
        else:
            numeric.append((n, mid))
    numeric.sort(reverse=True)
    non_numeric.sort(reverse=True)
    ordered = [mid for _, mid in numeric] + non_numeric

    failed = 0
    try:
        await handler.outbound.queue_delete_messages(
            chat_id,
            ordered,
            fire_and_forget=False,
        )
    except Exception as e:
        failed = len(ordered)
        logger.debug("Message delete failed for chat {}: {}", chat_id, type(e).__name__)

    if ordered:
        logger.info(
            "Clear delete attempted={} failed={}",
            len(ordered),
            failed,
        )


async def handle_clear_command(
    handler: MessagingCommandContext, incoming: IncomingMessage
) -> None:
    """
    Handle /clear command.

    Reply-scoped: delete the selected message and its literal reply subtree.
    Standalone: reset and delete the invoking chat's managed conversation.
    """
    if incoming.is_reply() and incoming.reply_to_message_id:
        result = await handler.clear_reply(
            incoming.scope,
            incoming.reply_to_message_id,
        )
        if result is None:
            msg_id = await handler.outbound.queue_send_message(
                incoming.chat_id,
                handler.format_status(
                    "🗑", "Cleared.", "Nothing to clear for that message."
                ),
                fire_and_forget=False,
                message_thread_id=incoming.message_thread_id,
            )
            handler.record_outgoing_message(
                incoming.platform, incoming.chat_id, msg_id, "command"
            )
            return

        delete_message_ids = set(result.delete_message_ids)
        if incoming.message_id is not None:
            delete_message_ids.add(str(incoming.message_id))
        await _delete_message_ids(handler, incoming.chat_id, delete_message_ids)
        handler.forget_tracked_message_ids(
            incoming.platform,
            incoming.chat_id,
            delete_message_ids,
        )
        return

    msg_ids = set(await handler.clear_chat(incoming.platform, incoming.chat_id))

    # Also delete the command message itself.
    await _delete_message_ids(handler, incoming.chat_id, msg_ids)


def _discover_installed_skills() -> list[dict[str, Any]]:
    """Scan local skill directories for installed skills."""
    from pathlib import Path

    skill_roots = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".gemini" / "skills",
        Path.home() / ".fcc" / "skills",
    ]
    discovered: dict[str, dict[str, Any]] = {}

    for root in skill_roots:
        if not root.is_dir():
            continue
        try:
            for item in root.iterdir():
                if item.name.startswith("."):
                    continue
                skill_dir = item.resolve() if item.is_symlink() else item
                if not skill_dir.is_dir():
                    continue

                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue

                name = item.name
                description = "No description provided."
                user_only = False
                argument_hint = ""
                body = ""

                try:
                    content = skill_md.read_text(encoding="utf-8", errors="replace")
                    if content.startswith("---"):
                        parts = content.split("---", 2)
                        if len(parts) >= 3:
                            yaml_text = parts[1]
                            body = parts[2].strip()
                            for line in yaml_text.splitlines():
                                line_str = line.strip()
                                if line_str.startswith("name:"):
                                    extracted = line_str.split(":", 1)[1].strip().strip("\"'")
                                    if extracted:
                                        name = extracted
                                elif line_str.startswith("description:"):
                                    extracted = line_str.split(":", 1)[1].strip().strip("\"'")
                                    if extracted:
                                        description = extracted
                                elif line_str.startswith("argument-hint:"):
                                    extracted = line_str.split(":", 1)[1].strip().strip("\"'")
                                    if extracted:
                                        argument_hint = extracted
                                elif line_str.startswith("disable-model-invocation:"):
                                    val = line_str.split(":", 1)[1].strip().lower()
                                    if val == "true":
                                        user_only = True
                    else:
                        body = content.strip()
                except Exception:
                    pass

                if name not in discovered:
                    discovered[name] = {
                        "name": name,
                        "description": description,
                        "user_only": user_only,
                        "argument_hint": argument_hint,
                        "body": body,
                    }
        except Exception:
            pass

    return sorted(discovered.values(), key=lambda s: str(s["name"]).lower())


def render_skills_page(page: int = 1) -> tuple[str, Any]:
    """Render /skills paginated view with Telegram InlineKeyboardMarkup."""
    skills = _discover_installed_skills()
    if not skills:
        return ("🧩 *已安裝技能列表*\n\n系統中未找到任何已安裝的技能。", None)

    per_page = 10
    total_pages = (len(skills) + per_page - 1) // per_page
    page = max(1, min(page, total_pages)) if total_pages > 0 else 1

    start_idx = (page - 1) * per_page
    page_skills = skills[start_idx : start_idx + per_page]

    lines = [f"🧩 *已安裝技能列表 (第 {page}/{total_pages} 頁 - 共 {len(skills)} 個)*\n"]

    for s in page_skills:
        name = escape_md_v2(str(s["name"]))
        desc = escape_md_v2(str(s["description"]))
        user_only = bool(s["user_only"])

        badge_text = "👤 僅限使用者" if user_only else "🤖 模型可用"
        badge = escape_md_v2(f"[{badge_text}]")

        lines.append(f"• */{name}* {badge}\n  {desc}")

    lines.append("\n💡 *提示：點擊下方按鈕翻頁，或發送 /skills <名稱> 查看規格。*")

    markup = None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        buttons = []
        if page > 1:
            buttons.append(
                InlineKeyboardButton(
                    "◀️ 上一頁", callback_data=f"skills_page:{page-1}"
                )
            )
        buttons.append(
            InlineKeyboardButton(
                f"{page} / {total_pages}", callback_data="skills_noop"
            )
        )
        if page < total_pages:
            buttons.append(
                InlineKeyboardButton(
                    "下一頁 ▶️", callback_data=f"skills_page:{page+1}"
                )
            )
        markup = InlineKeyboardMarkup([buttons])
    except Exception:
        pass

    return ("\n".join(lines), markup)


def render_skill_detail(skill_name: str) -> tuple[str, Any]:
    """Render /skills <name> detail view with Telegram InlineKeyboardMarkup."""
    skills = _discover_installed_skills()
    target_name = skill_name.lower().lstrip("/")
    target_skill = next(
        (s for s in skills if str(s["name"]).lower() == target_name), None
    )

    if not target_skill:
        return (f"❌ 找不到技能 '/{escape_md_v2(target_name)}'。", None)

    name = escape_md_v2(str(target_skill["name"]))
    desc = escape_md_v2(str(target_skill["description"]))
    hint = escape_md_v2(str(target_skill.get("argument_hint", "")))
    user_only = bool(target_skill["user_only"])
    body = str(target_skill.get("body", ""))[:500]

    badge_text = (
        "👤 僅限使用者 (disable-model-invocation: true)"
        if user_only
        else "🤖 模型可用 (Model-invocable)"
    )

    lines = [
        f"🧩 *技能詳細規格：/{name}*",
        "",
        "*權限狀態：* " + escape_md_v2(badge_text),
        "*技能描述：* " + desc,
    ]
    if hint:
        lines.append("*參數提示：* " + hint)
    if body:
        lines.extend(["", "*指引預覽：*", f"`{body}`"])

    lines.extend(["", "💡 發送 Prompt 即刻開始。"])

    markup = None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        buttons = [
            InlineKeyboardButton(
                "🔙 返回技能列表", callback_data="skills_page:1"
            )
        ]
        markup = InlineKeyboardMarkup([buttons])
    except Exception:
        pass

    return ("\n".join(lines), markup)


async def handle_skills_command(
    handler: MessagingCommandContext, incoming: IncomingMessage
) -> None:
    """Handle /skills command with Inline Keyboard Buttons and detail views."""
    parts = (incoming.text or "").strip().split()
    sub_arg = parts[1] if len(parts) > 1 else None

    if sub_arg is not None and not sub_arg.isdigit():
        msg_text, markup = render_skill_detail(sub_arg)
    else:
        page = int(sub_arg) if (sub_arg and sub_arg.isdigit()) else 1
        msg_text, markup = render_skills_page(page)

    msg_id = await handler.outbound.queue_send_message(
        incoming.chat_id,
        msg_text,
        fire_and_forget=False,
        message_thread_id=incoming.message_thread_id,
        reply_markup=markup,
    )
    handler.record_outgoing_message(
        incoming.platform, incoming.chat_id, msg_id, "command"
    )





