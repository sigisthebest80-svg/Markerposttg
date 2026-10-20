#!/usr/bin/env python3
"""
Telegram Channel & Group Post Maker Bot
Supports Channels, Supergroups & Groups.
Features: Emoji buttons, button color styling, clean cancel, target removal.
Render Web Service Ready — includes HTTP health-check server.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiosqlite
from aiohttp import web
from telegram import (
    ChatMemberAdministrator,
    ChatMemberOwner,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN = "8552889682:AAEsQ47kyzIV_VEBd90DHwC2satwQXBcMDA"

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

DB_FILE = os.getenv("DB_FILE", "bot_data.db")
PORT = int(os.getenv("PORT", "8080"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("PostMakerBot")
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# HTTP health-check server (needed for Render Web Service)
# ---------------------------------------------------------------------------
async def _health_handler(request: web.Request) -> web.Response:
    return web.Response(text="🤖 Post Maker Bot is running!", status=200)


async def _start_health_server() -> None:
    """Start a tiny aiohttp server on $PORT so Render keeps the service alive."""
    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/healthz", _health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("🌐 Health-check HTTP server listening on 0.0.0.0:%s", PORT)


# ---------------------------------------------------------------------------
# Button Builder — supports style= gracefully
# ---------------------------------------------------------------------------
def btn(
    text: str,
    callback_data: Optional[str] = None,
    url: Optional[str] = None,
    style: Optional[str] = None,
) -> InlineKeyboardButton:
    kwargs: Dict[str, Any] = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url

    if style:
        try:
            return InlineKeyboardButton(**kwargs, style=style)
        except TypeError:
            pass
    return InlineKeyboardButton(**kwargs)


COLOR_EMOJI = {"primary": "🔵", "success": "🟢", "danger": "🔴"}


# ---------------------------------------------------------------------------
# Message tracking helper — used for clean cancel
# ---------------------------------------------------------------------------
def track_msg(context: ContextTypes.DEFAULT_TYPE, message_id: Optional[int]) -> None:
    if not message_id:
        return
    lst = context.user_data.setdefault("_trash", [])
    if message_id not in lst:
        lst.append(message_id)


async def send_tracked(target, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    sent = await target.reply_text(text, **kwargs)
    track_msg(context, sent.message_id)
    return sent


async def purge_trash(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    ids = context.user_data.get("_trash", [])
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except TelegramError:
            pass
    context.user_data.pop("_trash", None)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
async def init_db() -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_title TEXT NOT NULL,
                chat_type TEXT NOT NULL DEFAULT 'channel',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, channel_id)
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_title TEXT NOT NULL,
                post_type TEXT NOT NULL,
                content_preview TEXT NOT NULL,
                published_at TEXT NOT NULL,
                telegram_message_id INTEGER NOT NULL
            );
            """
        )
        async with db.execute("PRAGMA table_info(channels);") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
            if "chat_type" not in columns:
                await db.execute(
                    "ALTER TABLE channels ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'channel';"
                )
        await db.commit()


async def save_channel(user_id: int, channel_id: int, title: str, chat_type: str) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO channels (user_id, channel_id, channel_title, chat_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel_id) DO UPDATE SET
                channel_title = excluded.channel_title,
                chat_type = excluded.chat_type;
            """,
            (user_id, channel_id, title, chat_type, now),
        )
        await db.commit()


async def delete_channel(user_id: int, channel_id: int) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "DELETE FROM channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        )
        await db.commit()


async def get_user_channels(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_id, channel_title, chat_type FROM channels WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def get_user_channel(user_id: int, channel_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT channel_id, channel_title, chat_type FROM channels WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def save_published_post(
    user_id: int,
    channel_id: int,
    channel_title: str,
    post_type: str,
    content_preview: str,
    telegram_message_id: int,
) -> None:
    async with aiosqlite.connect(DB_FILE) as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO posts (user_id, channel_id, channel_title, post_type, content_preview, published_at, telegram_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, channel_id, channel_title, post_type, content_preview[:120], now, telegram_message_id),
        )
        await db.commit()


async def get_user_posts(user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT channel_title, post_type, content_preview, published_at, telegram_message_id
            FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT ?
            """,
            (user_id, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ("http", "https", "tg"):
        return False
    if parsed.scheme.lower() in ("http", "https") and not parsed.netloc:
        return False
    return True


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------
(
    STATE_SET_CHANNEL,
    STATE_SELECT_CHANNEL,
    STATE_SELECT_TYPE,
    STATE_ENTER_CONTENT,
    STATE_ENTER_CAPTION,
    STATE_ADD_BUTTONS,
    STATE_SELECT_BUTTON_COLOR,
    STATE_CONFIRM_PUBLISH,
) = range(8)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------
def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("⚙️ Set Target", callback_data="menu_set_channel", style="primary"),
         btn("📝 Create Post", callback_data="menu_create_post", style="success")],
        [btn("📋 My Targets", callback_data="menu_my_channels", style="primary"),
         btn("📚 History", callback_data="menu_my_posts", style="primary")],
        [btn("❓ Help & Guide", callback_data="menu_help", style="primary")],
    ])


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")]
    ])


def kb_skip_cancel(skip_cb: str, skip_label: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(skip_label, callback_data=skip_cb, style="primary")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])


def build_post_markup(buttons: List[List[Dict[str, str]]]) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    keyboard = []
    for row in buttons:
        btn_row = [btn(text=b["text"], url=b["url"], style=b.get("style")) for b in row]
        if btn_row:
            keyboard.append(btn_row)
    return InlineKeyboardMarkup(keyboard) if keyboard else None


# ---------------------------------------------------------------------------
# Permission checks
# ---------------------------------------------------------------------------
async def check_bot_posting_permission(bot, target_id: int) -> tuple[bool, str]:
    try:
        member = await bot.get_chat_member(chat_id=target_id, user_id=bot.id)
        if isinstance(member, ChatMemberOwner):
            return True, "Owner"
        if isinstance(member, ChatMemberAdministrator):
            can_post = getattr(member, "can_post_messages", None)
            if can_post is False:
                return False, "Bot is an Admin but lacks 'Post Messages' permission."
            return True, "Administrator"
        return False, "Bot is not an Administrator in this Channel/Group."
    except BadRequest as e:
        if "chat not found" in e.message.lower():
            return False, "Bot is not added to this chat. Please make the bot an Admin first!"
        return False, f"Telegram error: {e.message}"
    except TelegramError as e:
        return False, f"Telegram API error: {e.message}"
    except Exception as e:
        logger.error("Error checking bot permission: %s", e)
        return False, "Verification failed. Please check the ID."


async def verify_user_channel_ownership(bot, target_id: int, user_id: int) -> tuple[bool, str]:
    try:
        user_member = await bot.get_chat_member(chat_id=target_id, user_id=user_id)
        if isinstance(user_member, ChatMemberOwner):
            return True, "Owner"
        if isinstance(user_member, ChatMemberAdministrator):
            return True, "Administrator"
        return False, "You are not an Owner or Admin of this Channel/Group."
    except BadRequest as e:
        if "user not found" in e.message.lower() or "participant" in e.message.lower():
            return False, "You are not a member/admin of this Channel/Group."
        return False, f"Verification failed: {e.message}"
    except Exception as e:
        logger.error("Error checking user ownership: %s", e)
        return False, "User verification error."


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    welcome_text = (
        "👋 <b>Welcome to Post Maker Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Publish professional posts to your <b>Channels</b>\n"
        "and <b>Groups</b> — text, photo, video, and colored URL buttons.\n\n"
        "🔐 <b>Security:</b> Only add targets where you are the\n"
        "<b>Owner / Admin</b>.\n"
        "🎨 <b>Colors:</b> Primary · Success · Danger\n"
        "😀 <b>Emoji:</b> Use any emoji in button labels"
    )
    if update.message:
        await update.message.reply_text(
            welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML
        )
    elif update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML
            )
        except BadRequest:
            await update.callback_query.message.reply_text(
                welcome_text, reply_markup=kb_main_menu(), parse_mode=ParseMode.HTML
            )
    return ConversationHandler.END


async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    help_text = (
        "📖 <b>Help & Guide</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🔹 How to get a Channel / Group ID?</b>\n"
        "1. Make the bot an <b>Admin</b> in your target.\n"
        "2. Forward any post to <b>@username_to_id_bot</b>.\n"
        "3. You'll get the numeric ID — e.g. <code>-1001234567890</code>.\n\n"
        "<b>🔹 How to create a post?</b>\n"
        "1. <b>Create Post</b> → select a target.\n"
        "2. Choose a format (Text / Photo / Video).\n"
        "3. Add content + caption.\n"
        "4. Add URL buttons and choose their <b>color</b>.\n"
        "5. Tap <b>🚀 Publish Now</b>.\n\n"
        "<b>🔹 Button format:</b>\n"
        "<code>Label - https://link.com</code>\n"
        "Two buttons in one row: <code>A - url | B - url</code>\n"
        "Emoji allowed: <code>🔥 Join - https://t.me/x</code>"
    )
    await query.edit_message_text(
        help_text,
        reply_markup=InlineKeyboardMarkup([
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")]
        ]),
        parse_mode=ParseMode.HTML,
    )


async def _render_my_channels(query, user_id: int) -> None:
    channels = await get_user_channels(user_id)

    if not channels:
        msg = (
            "📋 <b>My Targets</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "No targets added yet.\n\n"
            "👉 Start with <b>Set Target</b>."
        )
        keyboard = InlineKeyboardMarkup([
            [btn("⚙️ Set Target", callback_data="menu_set_channel", style="primary")],
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")],
        ])
    else:
        msg = "📋 <b>My Verified Targets</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, ch in enumerate(channels, 1):
            title = html.escape(ch.get("channel_title") or "Unnamed")
            c_type = ch.get("chat_type", "Channel").capitalize()
            icon = "📢" if c_type == "Channel" else "👥"
            msg += f"{icon} <b>{idx}. {title}</b>\n<code>ID: {ch['channel_id']}</code>\n\n"

        keyboard = InlineKeyboardMarkup([
            [btn("⚙️ Add New", callback_data="menu_set_channel", style="primary"),
             btn("🗑 Remove", callback_data="remove_target_menu", style="danger")],
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")],
        ])

    await query.edit_message_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def my_channels_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _render_my_channels(query, query.from_user.id)


async def remove_target_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    channels = await get_user_channels(query.from_user.id)

    if not channels:
        await query.answer("No targets to remove.", show_alert=True)
        return
    await query.answer()

    keyboard = []
    for ch in channels:
        icon = "📢" if ch.get("chat_type") == "Channel" else "👥"
        keyboard.append([
            btn(f"🗑 {icon} {ch['channel_title']}",
                callback_data=f"del_chan_{ch['channel_id']}",
                style="danger")
        ])
    keyboard.append([btn("🔙 Back", callback_data="menu_my_channels", style="primary")])

    await query.edit_message_text(
        "🗑 <b>Remove Target</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Tap the target you want to remove:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def delete_channel_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        channel_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.", reply_markup=kb_main_menu())
        return

    channel = await get_user_channel(query.from_user.id, channel_id)
    if not channel:
        await query.answer("Target not found.", show_alert=True)
        return

    icon = "📢" if channel["chat_type"] == "Channel" else "👥"
    keyboard = InlineKeyboardMarkup([
        [btn("✅ Yes, Remove", callback_data=f"confirm_del_{channel_id}", style="danger")],
        [btn("🔙 Cancel", callback_data="menu_my_channels", style="primary")],
    ])

    await query.edit_message_text(
        "⚠️ <b>Confirm Removal</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>{html.escape(channel['channel_title'])}</b>\n"
        f"🆔 <code>{channel['channel_id']}</code>\n\n"
        "Are you sure you want to remove this target?",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )


async def confirm_delete_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        channel_id = int(query.data.split("_")[2])
    except (IndexError, ValueError):
        await query.answer("Invalid.", show_alert=True)
        return

    await delete_channel(query.from_user.id, channel_id)
    await query.answer("✅ Target removed.", show_alert=False)
    await _render_my_channels(query, query.from_user.id)


async def my_posts_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    posts = await get_user_posts(query.from_user.id, limit=10)

    if not posts:
        msg = (
            "📚 <b>Post History</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "No posts published yet."
        )
    else:
        msg = "📚 <b>Last 10 Published Posts</b>\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        for idx, post in enumerate(posts, 1):
            ch_title = html.escape(post.get("channel_title") or "Target")
            p_type = post.get("post_type", "").capitalize()
            preview = html.escape(post.get("content_preview") or "")
            pub_date = post.get("published_at", "")[:16].replace("T", " ")
            msg += (
                f"<b>{idx}. [{p_type}]</b> → {ch_title}\n"
                f"🗓 <code>{pub_date} UTC</code>\n"
                f"💬 <i>{preview}</i>\n\n"
            )

    await query.edit_message_text(
        msg,
        reply_markup=InlineKeyboardMarkup([
            [btn("🔙 Back to Menu", callback_data="main_menu", style="primary")]
        ]),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Flow 1: Set Target
# ---------------------------------------------------------------------------
async def set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    msg = (
        "⚙️ <b>Set Target — Channel / Group</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Steps:</b>\n"
        "1️⃣ Make the bot an <b>Administrator</b> in your target.\n"
        "2️⃣ Copy the numeric ID of that channel/group.\n"
        "3️⃣ Paste it below and send.\n\n"
        "📌 <b>Format:</b> <code>-1001234567890</code>"
    )
    await query.edit_message_text(msg, reply_markup=kb_cancel(), parse_mode=ParseMode.HTML)
    return STATE_SET_CHANNEL


async def set_channel_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    user_id = update.effective_user.id
    text_val = (update.message.text or "").strip()

    if text_val.startswith("@") or not (text_val.startswith("-") or text_val.isdigit()):
        await send_tracked(
            update.message, context,
            "❌ <b>Invalid input</b>\n\n"
            "Please send only a numeric Chat ID — e.g. <code>-1001234567890</code>.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    try:
        target_id = int(text_val)
    except ValueError:
        await send_tracked(
            update.message, context,
            "❌ Please send a valid numeric ID.",
            reply_markup=kb_cancel(),
        )
        return STATE_SET_CHANNEL

    try:
        chat = await context.bot.get_chat(target_id)
    except BadRequest as e:
        await send_tracked(
            update.message, context,
            f"❌ <b>Chat not found</b>\n\n{html.escape(e.message)}\n\n"
            "Please confirm that the bot is an <b>Admin</b> in that channel/group.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    if chat.type not in ("channel", "supergroup", "group"):
        await send_tracked(
            update.message, context,
            f"❌ This is a <b>{chat.type}</b>. Only Channels and Groups are supported.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    bot_ok, bot_reason = await check_bot_posting_permission(context.bot, target_id)
    if not bot_ok:
        await send_tracked(
            update.message, context,
            f"❌ <b>Bot permission missing</b>\n\n{html.escape(bot_reason)}\n\n"
            "Please make the bot an Admin and enable the <b>Post Messages</b> permission.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    user_ok, user_reason = await verify_user_channel_ownership(context.bot, target_id, user_id)
    if not user_ok:
        await send_tracked(
            update.message, context,
            f"⛔ <b>Access Denied</b>\n\n{html.escape(user_reason)}\n\n"
            "You can only add your own channel/group.",
            reply_markup=kb_cancel(), parse_mode=ParseMode.HTML,
        )
        return STATE_SET_CHANNEL

    chat_type_str = "Group" if chat.type in ("group", "supergroup") else "Channel"
    await save_channel(user_id, target_id, chat.title or chat_type_str, chat_type_str)

    await purge_trash(context, update.effective_chat.id)

    icon = "📢" if chat_type_str == "Channel" else "👥"
    await update.message.reply_text(
        f"✅ <b>Target Linked</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{icon} <b>{html.escape(chat.title or 'Unnamed')}</b>\n"
        f"🆔 <code>{target_id}</code>\n"
        f"👤 <b>Your role:</b> {user_reason}\n\n"
        "You can now post to this target!",
        reply_markup=kb_main_menu(),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Flow 2: Create Post
# ---------------------------------------------------------------------------
async def create_post_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    channels = await get_user_channels(query.from_user.id)

    if not channels:
        await query.edit_message_text(
            "⚠️ <b>No target added</b>\n\n"
            "Please add a Channel or Group before creating a post.",
            reply_markup=InlineKeyboardMarkup([
                [btn("⚙️ Set Target", callback_data="menu_set_channel", style="primary")],
                [btn("🔙 Main Menu", callback_data="main_menu", style="primary")],
            ]),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    keyboard = []
    for ch in channels:
        icon = "📢" if ch.get("chat_type") == "Channel" else "👥"
        keyboard.append([
            btn(f"{icon} {ch['channel_title']}",
                callback_data=f"sel_chan_{ch['channel_id']}",
                style="primary")
        ])
    keyboard.append([btn("❌ Cancel", callback_data="cancel_action", style="danger")])

    await query.edit_message_text(
        "📝 <b>Create Post — 1/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Which target do you want to post to?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return STATE_SELECT_CHANNEL


async def select_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        channel_id = int(data.split("_")[2])
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    channel = await get_user_channel(query.from_user.id, channel_id)
    if not channel:
        await query.edit_message_text(
            "⛔ This target is not linked to your account.",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    context.user_data["draft"] = {
        "channel_id": channel["channel_id"],
        "channel_title": channel["channel_title"],
        "chat_type": channel.get("chat_type", "Channel"),
        "post_type": None,
        "text": None,
        "media_file_id": None,
        "caption": None,
        "buttons": [],
        "button_style": None,
    }

    keyboard = InlineKeyboardMarkup([
        [btn("📄 Text", callback_data="post_type_text", style="primary"),
         btn("🖼 Photo", callback_data="post_type_photo", style="primary"),
         btn("🎥 Video", callback_data="post_type_video", style="primary")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])

    await query.edit_message_text(
        "📝 <b>Create Post — 2/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 <b>Target:</b> {html.escape(channel['channel_title'])}\n\n"
        "Choose the post format:",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return STATE_SELECT_TYPE


async def select_post_type_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    post_type = query.data.replace("post_type_", "")

    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    draft["post_type"] = post_type

    prompts = {
        "text": ("📝 <b>Create Post — 3/6</b>\n"
                 "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                 "Send your message text.\n"
                 "<i>HTML supported — &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;</i>"),
        "photo": ("🖼 <b>Create Post — 3/6</b>\n"
                  "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  "Upload a photo.\n"
                  "<i>You can include a caption too.</i>"),
        "video": ("🎥 <b>Create Post — 3/6</b>\n"
                  "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                  "Upload a video.\n"
                  "<i>You can include a caption too.</i>"),
    }

    await query.edit_message_text(
        prompts.get(post_type, "Send content:"),
        reply_markup=kb_cancel(),
        parse_mode=ParseMode.HTML,
    )
    return STATE_ENTER_CONTENT


async def enter_content_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    post_type = draft["post_type"]

    if post_type == "text":
        if not update.message.text:
            await send_tracked(update.message, context, "❌ Please send a text message.", reply_markup=kb_cancel())
            return STATE_ENTER_CONTENT
        draft["text"] = update.message.text_html
        return await ask_button_step(update, context)

    if post_type == "photo":
        if not update.message.photo:
            await send_tracked(update.message, context, "❌ Please send a photo.", reply_markup=kb_cancel())
            return STATE_ENTER_CONTENT
        draft["media_file_id"] = update.message.photo[-1].file_id
        if update.message.caption:
            draft["caption"] = update.message.caption_html
            return await ask_button_step(update, context)
        return await ask_caption_step(update, context)

    if post_type == "video":
        if not update.message.video:
            await send_tracked(update.message, context, "❌ Please send a video.", reply_markup=kb_cancel())
            return STATE_ENTER_CONTENT
        draft["media_file_id"] = update.message.video.file_id
        if update.message.caption:
            draft["caption"] = update.message.caption_html
            return await ask_button_step(update, context)
        return await ask_caption_step(update, context)

    return ConversationHandler.END


async def ask_caption_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await send_tracked(
        update.message, context,
        "📝 <b>Create Post — 4/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Do you want to add a caption?\n"
        "Send text or tap <b>Skip</b>.",
        reply_markup=kb_skip_cancel("skip_caption", "⏭ Skip Caption"),
        parse_mode=ParseMode.HTML,
    )
    return STATE_ENTER_CAPTION


async def enter_caption_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    if update.message.text:
        draft["caption"] = update.message.text_html

    return await ask_button_step(update, context)


async def skip_caption_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END
    draft["caption"] = None
    return await ask_button_step(query, context, is_query=True)


async def ask_button_step(source: Any, context: ContextTypes.DEFAULT_TYPE, is_query: bool = False) -> int:
    msg = (
        "🔗 <b>Create Post — 5/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>URL Buttons (optional)</b>\n\n"
        "Format: <code>Label - https://link.com</code>\n\n"
        "<b>Examples:</b>\n"
        "• 1 button: <code>Website - https://example.com</code>\n"
        "• Emoji allowed: <code>🔥 Join - https://t.me/x</code>\n"
        "• 2 in one row: <code>Site - https://a.com | Join - https://t.me/x</code>\n\n"
        "If you don't want buttons, tap <b>Skip</b>."
    )
    keyboard = kb_skip_cancel("skip_buttons", "⏭ Skip Buttons")

    if is_query:
        await source.edit_message_text(msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    else:
        await send_tracked(source.message, context, msg, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    return STATE_ADD_BUTTONS


async def add_buttons_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    track_msg(context, update.message.message_id)
    draft = context.user_data.get("draft")
    if not draft:
        await update.message.reply_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    text = update.message.text or ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    rows: List[List[Dict[str, str]]] = []

    for line in lines:
        row_items = []
        for raw_btn in line.split("|"):
            parts = re.split(r"\s+-\s+", raw_btn.strip(), maxsplit=1)
            if len(parts) != 2:
                await send_tracked(
                    update.message, context,
                    f"❌ <b>Wrong format</b>\n\n"
                    f"Correct: <code>Label - https://link.com</code>\n"
                    f"You sent: <code>{html.escape(raw_btn.strip())}</code>",
                    parse_mode=ParseMode.HTML,
                )
                return STATE_ADD_BUTTONS

            btn_label = parts[0].strip()
            btn_url = parts[1].strip()

            if not is_valid_url(btn_url):
                await send_tracked(
                    update.message, context,
                    f"❌ <b>Invalid URL</b>: <code>{html.escape(btn_url)}</code>\n\n"
                    "Must start with <code>http://</code>, <code>https://</code> or <code>tg://</code>",
                    parse_mode=ParseMode.HTML,
                )
                return STATE_ADD_BUTTONS

            row_items.append({"text": btn_label, "url": btn_url})
        if row_items:
            rows.append(row_items)

    draft["buttons"] = rows

    keyboard = InlineKeyboardMarkup([
        [btn("🔵 Primary", callback_data="btncolor_primary", style="primary"),
         btn("🟢 Success", callback_data="btncolor_success", style="success"),
         btn("🔴 Danger", callback_data="btncolor_danger", style="danger")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])

    await send_tracked(
        update.message, context,
        "🎨 <b>Create Post — 6/6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Buttons created! Now choose their <b>color</b>:\n\n"
        "🔵 <b>Primary</b> — blue tone\n"
        "🟢 <b>Success</b> — green tone\n"
        "🔴 <b>Danger</b> — red tone",
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
    )
    return STATE_SELECT_BUTTON_COLOR


async def skip_buttons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END
    draft["buttons"] = []
    draft["button_style"] = None
    return await show_preview(query, context, is_query=True)


async def select_button_color_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    style = query.data.replace("btncolor_", "")
    draft = context.user_data.get("draft")
    if not draft:
        await query.edit_message_text("❌ Session expired.", reply_markup=kb_main_menu())
        return ConversationHandler.END

    draft["button_style"] = style
    for row in draft.get("buttons", []):
        for b in row:
            b["style"] = style

    return await show_preview(query, context, is_query=True)


# ---------------------------------------------------------------------------
# Preview & Publish
# ---------------------------------------------------------------------------
async def show_preview(source: Any, context: ContextTypes.DEFAULT_TYPE, is_query: bool = False) -> int:
    draft = context.user_data.get("draft")
    if not draft:
        return ConversationHandler.END

    post_type = draft["post_type"]
    post_markup = build_post_markup(draft.get("buttons", []))

    target_msg = source.message

    header = (
        "👁️ <b>Post Preview</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target:</b> {html.escape(draft['channel_title'])} "
        f"(<code>{draft['channel_id']}</code>)\n"
    )
    if draft.get("button_style"):
        header += f"🎨 <b>Button color:</b> {COLOR_EMOJI.get(draft['button_style'], '')} {draft['button_style'].capitalize()}\n"
    header += "━━━━━━━━━━━━━━━━━━━━━━━\n\nPreview below 👇"

    sent_header = await target_msg.reply_text(header, parse_mode=ParseMode.HTML)
    track_msg(context, sent_header.message_id)

    try:
        if post_type == "text":
            sent = await target_msg.reply_text(
                draft["text"],
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
                disable_web_page_preview=False,
            )
        elif post_type == "photo":
            sent = await target_msg.reply_photo(
                photo=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
            )
        elif post_type == "video":
            sent = await target_msg.reply_video(
                video=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML,
                reply_markup=post_markup,
            )
        else:
            sent = None
        if sent:
            track_msg(context, sent.message_id)
    except TelegramError as e:
        sent = await target_msg.reply_text(f"⚠️ Preview error: {html.escape(e.message)}")
        track_msg(context, sent.message_id)

    confirm_keyboard = InlineKeyboardMarkup([
        [btn("🚀 Publish Now", callback_data="publish_confirm", style="success")],
        [btn("❌ Cancel", callback_data="cancel_action", style="danger")],
    ])

    sent_confirm = await target_msg.reply_text(
        "Does everything look good?\nTap <b>🚀 Publish Now</b> to publish.",
        reply_markup=confirm_keyboard,
        parse_mode=ParseMode.HTML,
    )
    track_msg(context, sent_confirm.message_id)

    return STATE_CONFIRM_PUBLISH


async def publish_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    draft = context.user_data.get("draft")

    if not draft:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ Draft expired.",
            reply_markup=kb_main_menu(),
        )
        return ConversationHandler.END

    channel_id = draft["channel_id"]
    user_ok, _ = await verify_user_channel_ownership(context.bot, channel_id, user_id)
    if not user_ok:
        await purge_trash(context, update.effective_chat.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⛔ <b>Security Block</b>\n\nYou are no longer an Admin of this target.",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    post_type = draft["post_type"]
    post_markup = build_post_markup(draft.get("buttons", []))
    published_msg = None
    content_preview = ""

    try:
        if post_type == "text":
            published_msg = await context.bot.send_message(
                chat_id=channel_id, text=draft["text"],
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft["text"])
        elif post_type == "photo":
            published_msg = await context.bot.send_photo(
                chat_id=channel_id, photo=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft.get("caption") or "[Photo]")
        elif post_type == "video":
            published_msg = await context.bot.send_video(
                chat_id=channel_id, video=draft["media_file_id"],
                caption=draft.get("caption"),
                parse_mode=ParseMode.HTML, reply_markup=post_markup,
            )
            content_preview = re.sub(r"<[^>]*>", "", draft.get("caption") or "[Video]")
    except TelegramError as e:
        logger.error("Failed to post: %s", e.message)
        await purge_trash(context, update.effective_chat.id)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ <b>Publishing Failed</b>\n\n{html.escape(e.message)}",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
        return ConversationHandler.END

    if published_msg:
        await save_published_post(
            user_id=user_id, channel_id=channel_id,
            channel_title=draft["channel_title"],
            post_type=post_type, content_preview=content_preview,
            telegram_message_id=published_msg.message_id,
        )

    draft_title = draft["channel_title"]
    context.user_data.pop("draft", None)

    await purge_trash(context, update.effective_chat.id)

    try:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                "🎉 <b>Post Published Successfully!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🎯 <b>Target:</b> {html.escape(draft_title)}\n"
                f"🆔 <b>Message ID:</b> <code>{published_msg.message_id if published_msg else 'N/A'}</code>\n\n"
                "Main menu:"
            ),
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.error("Failed to send success message: %s", e)

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.message.delete()
        except TelegramError:
            pass
    elif update.message:
        try:
            await update.message.delete()
        except TelegramError:
            pass

    await purge_trash(context, chat_id)
    context.user_data.pop("draft", None)

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="✅ <b>Action cancelled.</b>\n\nMain menu:",
            reply_markup=kb_main_menu(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    token = BOT_TOKEN.strip()
    if not token:
        sys.stderr.write("CRITICAL ERROR: BOT_TOKEN is empty.\n")
        sys.exit(1)

    async def _post_init(app: Application) -> None:
        await init_db()
        await _start_health_server()

    app: Application = ApplicationBuilder().token(token).post_init(_post_init).build()

    create_post_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_post_start, pattern="^menu_create_post$")],
        states={
            STATE_SELECT_CHANNEL: [
                CallbackQueryHandler(select_channel_callback, pattern=r"^sel_chan_")
            ],
            STATE_SELECT_TYPE: [
                CallbackQueryHandler(select_post_type_callback, pattern=r"^post_type_")
            ],
            STATE_ENTER_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
                    enter_content_process,
                )
            ],
            STATE_ENTER_CAPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_caption_process),
                CallbackQueryHandler(skip_caption_callback, pattern="^skip_caption$"),
            ],
            STATE_ADD_BUTTONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_buttons_process),
                CallbackQueryHandler(skip_buttons_callback, pattern="^skip_buttons$"),
            ],
            STATE_SELECT_BUTTON_COLOR: [
                CallbackQueryHandler(select_button_color_callback, pattern="^btncolor_")
            ],
            STATE_CONFIRM_PUBLISH: [
                CallbackQueryHandler(publish_confirm_callback, pattern="^publish_confirm$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_action, pattern="^cancel_action$"),
            CommandHandler("cancel", cancel_action),
            CommandHandler("start", start_command),
        ],
        per_user=True,
        per_chat=True,
    )

    set_channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_channel_start, pattern="^menu_set_channel$")],
        states={
            STATE_SET_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, set_channel_process)
            ]
        },
        fallbacks=[
            CallbackQueryHandler(cancel_action, pattern="^cancel_action$"),
            CommandHandler("cancel", cancel_action),
            CommandHandler("start", start_command),
        ],
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_callback))
    app.add_handler(set_channel_conv)
    app.add_handler(create_post_conv)

    app.add_handler(CallbackQueryHandler(start_command, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(help_callback, pattern="^menu_help$"))
    app.add_handler(CallbackQueryHandler(my_channels_callback, pattern="^menu_my_channels$"))
    app.add_handler(CallbackQueryHandler(my_posts_callback, pattern="^menu_my_posts$"))
    app.add_handler(CallbackQueryHandler(remove_target_menu_callback, pattern="^remove_target_menu$"))
    app.add_handler(CallbackQueryHandler(delete_channel_prompt_callback, pattern=r"^del_chan_"))
    app.add_handler(CallbackQueryHandler(confirm_delete_channel_callback, pattern=r"^confirm_del_"))
    app.add_handler(CallbackQueryHandler(cancel_action, pattern="^cancel_action$"))

    logger.info("🚀 Bot starting (Render Web Service mode) on port %s ...", PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
