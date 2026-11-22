"""
Matrix Supreme - Telegram Group Moderation Bot
Requires: pip install "python-telegram-bot>=21" pillow
Run:
    BOT_TOKEN=123:ABC OWNER_IDS=111111,222222 python matrix_supreme_bot.py
Optional: DB_PATH=/data/matrix_supreme.db (use a persistent volume on Railway)

OWNER_IDS = Telegram user IDs allowed to use /fban and /unfban (global ban).
Make the bot an ADMIN in your group with: delete messages, ban users, pin messages,
and "manage tags" (for /addtag). In @BotFather: /setprivacy -> Disable.
"""
import asyncio
import html
import io
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from datetime import timedelta

from telegram import (ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters,
                      Update)
from telegram.constants import ChatMemberStatus
from telegram.ext import (Application, CallbackQueryHandler, ChatJoinRequestHandler,
                          ChatMemberHandler, CommandHandler, MessageHandler, filters)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
OWNER_IDS = {int(x) for x in os.environ.get("OWNER_IDS", "").replace(" ", "").split(",") if x}

MAX_WARNS = 3
FLOOD_MSGS = 6
FLOOD_SECS = 6
FLOOD_MUTE_MIN = 5
TAGS_PER_MESSAGE = 5

DB_PATH = os.environ.get("DB_PATH", "matrix_supreme.db")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER PRIMARY KEY, rules TEXT, welcome TEXT,
    links_ok INTEGER DEFAULT 0, flood_on INTEGER DEFAULT 1, locks TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS warns(chat_id INTEGER, user_id INTEGER, n INTEGER,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS words(chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id, word));
CREATE TABLE IF NOT EXISTS members(chat_id INTEGER, user_id INTEGER, username TEXT, name TEXT,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE IF NOT EXISTS fbans(user_id INTEGER PRIMARY KEY, reason TEXT);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    last_name TEXT);
CREATE TABLE IF NOT EXISTS filters(chat_id INTEGER, keyword TEXT, kind TEXT, file_id TEXT, body TEXT,
    PRIMARY KEY(chat_id, keyword));
CREATE TABLE IF NOT EXISTS user_quotes(chat_id INTEGER, user_id INTEGER, file_id TEXT,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS quotes(chat_id INTEGER, msg_id INTEGER, file_id TEXT,
    PRIMARY KEY(chat_id, msg_id));
""")
for _col, _ddl in [("welcome_on", "INTEGER DEFAULT 1"), ("goodbye_on", "INTEGER DEFAULT 0"),
                   ("goodbye", "TEXT"), ("clean_service", "INTEGER DEFAULT 0")]:
    try:
        db.execute(f"ALTER TABLE settings ADD COLUMN {_col} {_ddl}")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
flood = defaultdict(lambda: deque(maxlen=FLOOD_MSGS))
_admin_cache = {}

HELP = (
    "🛡 Matrix Supreme — Group Moderation\n\n"
    "Everyone: /rules, /help, /kickme (non-admins)\n\n"
    "Admins (reply to a user):\n"
    "/warn [reason], /unwarn, /warns\n"
    "/mute [minutes], /unmute\n"
    "/kick, /ban, /unban <user_id>\n"
    "/del, /pin, /unpin\n"
    "/tagall [message]\n"
    "/promote (reply) or /promote @username — make someone an admin\n\n"
    "Locks (admins):\n"
    "/lock all — only admins can talk\n"
    "/lock messages stickers gifs photos videos voice audio documents polls forwards "
    "contacts location media links invites\n"
    "/unlock <same options>, /locks\n\n"
    "Tags (admins):\n"
    "/addtag <group_id>\n@username - tag\n@username2 - tag2\n\n"
    "Settings (admins):\n"
    "/settings — toggle panel\n"
    "/setrules <text>, /setwelcome <text>, /setgoodbye <text>\n"
    "   placeholders: {name} {username} {group}\n"
    "/welcome on|off, /goodbye on|off\n"
    "/lockforward, /unlockforward\n"
    "/links on|off, /flood on|off\n"
    "/addword, /delword, /words\n\n"
    "Filters (admins):\n"
    "/filter <word> (reply to a message) — bot answers with that message whenever <word> is said\n"
    "/filter <word> <reply text>, /filters, /stop <word>, /stopall\n\n"
    "Quotes (everyone):\n"
    "/q (reply) — make a quote sticker; replying to that message later shows the quote\n"
    "/q r — include the replied-to message, /q @user — show the quote whenever @user is tagged\n"
    "/unq (admins, reply) — remove a saved quote\n\n"
    "Info: /id [reply | @user | id], /info [reply | @user | id]\n\n"
    "Bot owners: /fban (reply or user_id [reason]), /unfban <user_id>"
)

NO_PERMS = ChatPermissions(
    can_send_messages=False, can_send_audios=False, can_send_documents=False,
    can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
    can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
    can_add_web_page_previews=False, can_invite_users=True, can_change_info=False,
    can_pin_messages=False)
FULL_PERMS = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True, can_change_info=False,
    can_pin_messages=False)

LOCK_ALIASES = {
    "all": ["all"], "everything": ["all"],
    "message": ["text"], "messages": ["text"], "text": ["text"], "msg": ["text"], "chat": ["text"],
    "sticker": ["stickers"], "stickers": ["stickers"],
    "gif": ["gifs"], "gifs": ["gifs"],
    "photo": ["photos"], "photos": ["photos"], "image": ["photos"], "images": ["photos"],
    "video": ["videos"], "videos": ["videos"],
    "voice": ["voice"], "audio": ["audio"], "music": ["audio"],
    "doc": ["documents"], "docs": ["documents"], "document": ["documents"],
    "documents": ["documents"], "file": ["documents"], "files": ["documents"],
    "poll": ["polls"], "polls": ["polls"],
    "forward": ["forwards"], "forwards": ["forwards"],
    "contact": ["contacts"], "contacts": ["contacts"],
    "location": ["location"], "locations": ["location"],
    "media": ["photos", "videos", "gifs", "voice", "audio", "documents"],
    "link": ["links"], "links": ["links"], "url": ["links"], "urls": ["links"],
    "invite": ["invites"], "invites": ["invites"], "invitelink": ["invites"],
    "invitelinks": ["invites"],
}
INVITE_RE = re.compile(r"(t\.me|telegram\.me|telegram\.dog)/(\+|joinchat/)", re.I)


def has_invite(m):
    texts = [m.text or m.caption or ""]
    for e in tuple(m.entities or ()) + tuple(m.caption_entities or ()):
        if e.url:
            texts.append(e.url)
    return any(INVITE_RE.search(t) for t in texts)


LOCK_CHECKS = {
    "text": lambda m: bool(m.text) and not m.text.startswith("/"),
    "stickers": lambda m: bool(m.sticker),
    "gifs": lambda m: bool(m.animation),
    "photos": lambda m: bool(m.photo),
    "videos": lambda m: bool(m.video or m.video_note),
    "voice": lambda m: bool(m.voice),
    "audio": lambda m: bool(m.audio),
    "documents": lambda m: bool(m.document) and not m.animation,
    "polls": lambda m: bool(m.poll),
    "forwards": lambda m: bool(m.forward_origin),
    "contacts": lambda m: bool(m.contact),
    "location": lambda m: bool(m.location or m.venue),
    "invites": has_invite,
}


# ---------- helpers ----------
def q(sql, *args):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get(chat_id, col):
    q("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", chat_id)
    return q(f"SELECT {col} FROM settings WHERE chat_id=?", chat_id).fetchone()[0]


def put(chat_id, col, val):
    get(chat_id, col)
    q(f"UPDATE settings SET {col}=? WHERE chat_id=?", val, chat_id)


def get_locks(chat_id) -> set:
    return {x for x in (get(chat_id, "locks") or "").split(",") if x}


def put_locks(chat_id, locks: set):
    put(chat_id, "locks", ",".join(sorted(locks)))


async def is_admin(ctx, chat_id, user_id) -> bool:
    key = (chat_id, user_id)
    hit = _admin_cache.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        m = await ctx.bot.get_chat_member(chat_id, user_id)
        res = m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        res = False
    _admin_cache[key] = (time.time(), res)
    return res


async def admin_only(update: Update, ctx) -> bool:
    msg = update.message
    if msg.sender_chat and msg.sender_chat.id == update.effective_chat.id:  # anonymous admin
        return True
    if await is_admin(ctx, update.effective_chat.id, update.effective_user.id):
        return True
    await msg.reply_text("Admins only.")
    return False


async def get_target(update: Update, ctx):
    r = update.message.reply_to_message
    if not r:
        await update.message.reply_text("Reply to a user's message with this command.")
        return None
    t = r.from_user
    if await is_admin(ctx, update.effective_chat.id, t.id):
        await update.message.reply_text("I can't do that to an admin.")
        return None
    return t


async def tg_raw(method: str, **params):
    """Direct Bot API call (used for newer methods the library may not wrap yet)."""
    def _call():
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            return json.load(e)
    res = await asyncio.to_thread(_call)
    if not res.get("ok"):
        raise RuntimeError(res.get("description", "Telegram error"))
    return res["result"]


# Optional: resolve ANY @username through a helper user account (Telethon).
# Set TG_API_ID, TG_API_HASH (from my.telegram.org) and TG_SESSION (a Telethon StringSession).
_userclient = None


async def resolve_username(username: str):
    global _userclient
    api_id, api_hash, session = (os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH"),
                                 os.environ.get("TG_SESSION"))
    if not (api_id and api_hash and session):
        return None
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        if _userclient is None:
            _userclient = TelegramClient(StringSession(session), int(api_id), api_hash)
            await _userclient.connect()
        ent = await _userclient.get_entity(username)
        if getattr(ent, "bot", False) or not hasattr(ent, "first_name"):
            return None
        return ent.id, " ".join(x for x in (ent.first_name, ent.last_name) if x) or username
    except Exception as e:
        logging.warning("resolve_username failed: %s", e)
        return None


async def set_member_tag(ctx, chat_id, user_id, tag):
    if hasattr(ctx.bot, "set_chat_member_tag"):
        return await ctx.bot.set_chat_member_tag(chat_id, user_id, tag)
    return await tg_raw("setChatMemberTag", chat_id=chat_id, user_id=user_id, tag=tag)


def remember_user(u):
    if u and not u.is_bot:
        q("INSERT OR REPLACE INTO users VALUES(?,?,?,?)", u.id, (u.username or "").lower(),
          u.first_name or "", u.last_name or "")


def has_emoji(text: str) -> bool:
    return any(ord(c) > 0xFFFF or unicodedata.category(c) in ("So", "Cs") for c in text)


# ---------- tracking (members, chats, federation bans) ----------
async def tracker(update: Update, ctx):
    msg, chat = update.effective_message, update.effective_chat
    if not msg or chat.type not in ("group", "supergroup"):
        return
    q("INSERT OR REPLACE INTO chats VALUES(?,?)", chat.id, chat.title)
    users = list(msg.new_chat_members or [])
    if msg.from_user:
        users.append(msg.from_user)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        users.append(msg.reply_to_message.from_user)
    for u in users:
        if u.is_bot:
            continue
        remember_user(u)
        q("INSERT OR REPLACE INTO members VALUES(?,?,?,?)", chat.id, u.id,
          (u.username or "").lower(), u.full_name)
        if q("SELECT 1 FROM fbans WHERE user_id=?", u.id).fetchone():
            if not await is_admin(ctx, chat.id, u.id):
                try:
                    await ctx.bot.ban_chat_member(chat.id, u.id)
                    await ctx.bot.send_message(chat.id, f"🔨 {u.full_name} is federation-banned and was removed.")
                except Exception:
                    pass


async def chat_member_updates(update: Update, ctx):
    """Record people when they join/change status, even if they never send a message."""
    cm = update.chat_member
    if not cm or cm.chat.type not in ("group", "supergroup"):
        return
    u = cm.new_chat_member.user
    if u.is_bot:
        return
    if cm.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return
    remember_user(u)
    q("INSERT OR REPLACE INTO members VALUES(?,?,?,?)", cm.chat.id, u.id,
      (u.username or "").lower(), u.full_name)


async def join_request(update: Update, ctx):
    """Record people who request to join (private/approval groups)."""
    r = update.chat_join_request
    if r and not r.from_user.is_bot:
        u = r.from_user
        remember_user(u)
        q("INSERT OR REPLACE INTO members VALUES(?,?,?,?)", r.chat.id, u.id,
          (u.username or "").lower(), u.full_name)


async def my_chat_member(update: Update, ctx):
    ch, new = update.effective_chat, update.my_chat_member.new_chat_member
    if ch.type not in ("group", "supergroup"):
        return
    if new.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        q("INSERT OR REPLACE INTO chats VALUES(?,?)", ch.id, ch.title)
    else:
        q("DELETE FROM chats WHERE chat_id=?", ch.id)


# ---------- general ----------
async def start(update: Update, ctx):
    await update.message.reply_text(HELP)


async def rules(update: Update, ctx):
    text = get(update.effective_chat.id, "rules")
    await update.message.reply_text(text or "No rules set yet. Admins: /setrules <text>")


async def setrules(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        return await update.message.reply_text("Usage: /setrules <text>")
    put(update.effective_chat.id, "rules", text)
    await update.message.reply_text("✅ Rules updated.")


HTML_MARK = "<!--html-->"


def template_from_message(update: Update) -> str:
    """Template text from command args, or from the replied-to message (keeps its formatting)."""
    r = update.message.reply_to_message
    if r and (r.text or r.caption):
        return HTML_MARK + (r.text_html if r.text else r.caption_html)
    return update.message.text.partition(" ")[2].strip()


async def setwelcome(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    text = template_from_message(update)
    if not text:
        return await update.message.reply_text("Usage: /setwelcome Welcome {name} ({username}) to {group}! Read /rules.\n"
            "Placeholders: {name} = member's name, {username} = their @username, {group} = group name")
    put(update.effective_chat.id, "welcome", text)
    await update.message.reply_text("✅ Welcome message updated.")


def render(template: str, user, chat) -> str:
    """Fill {name}, {username}, {group} in welcome/goodbye templates."""
    uname = f"@{user.username}" if user.username else html.escape(user.full_name)
    body = template[len(HTML_MARK):] if template.startswith(HTML_MARK) else html.escape(template)
    for key, val in (("name", user.mention_html()), ("username", uname),
                     ("group", html.escape(chat.title or "the group"))):
        body = re.sub(r"\{" + key + r"\}", lambda _m, v=val: v, body, flags=re.I)
    return body


async def welcome(update: Update, ctx):
    cid = update.effective_chat.id
    if not get(cid, "welcome_on"):
        return
    template = get(cid, "welcome") or "Welcome {name} to {group}! 👋 Please read /rules."
    for u in update.message.new_chat_members:
        if u.is_bot or q("SELECT 1 FROM fbans WHERE user_id=?", u.id).fetchone():
            continue
        await ctx.bot.send_message(cid, render(template, u, update.effective_chat), parse_mode="HTML")


async def goodbye(update: Update, ctx):
    cid = update.effective_chat.id
    u = update.message.left_chat_member
    if not u or u.is_bot or not get(cid, "goodbye_on"):
        return
    template = get(cid, "goodbye") or "Goodbye {name} 👋"
    await ctx.bot.send_message(cid, render(template, u, update.effective_chat), parse_mode="HTML")


async def setgoodbye(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    text = template_from_message(update)
    if not text:
        return await update.message.reply_text("Usage: /setgoodbye Goodbye {name}, we'll miss you in {group}!\n"
            "Placeholders: {name} = member's name, {username} = their @username, {group} = group name")
    put(update.effective_chat.id, "goodbye", text)
    await update.message.reply_text("✅ Goodbye message updated. Turn it on with /goodbye on")


async def _onoff(update: Update, ctx, col, label):
    if not await admin_only(update, ctx):
        return
    cid = update.effective_chat.id
    if ctx.args and ctx.args[0].lower() in ("on", "off"):
        put(cid, col, int(ctx.args[0].lower() == "on"))
    await update.message.reply_text(
        f"{label} is {'ON ✅' if get(cid, col) else 'OFF ❌'}. Use /{label.lower()} on|off")


async def welcome_cmd(update: Update, ctx):
    await _onoff(update, ctx, "welcome_on", "Welcome")


async def goodbye_cmd(update: Update, ctx):
    await _onoff(update, ctx, "goodbye_on", "Goodbye")


async def kickme(update: Update, ctx):
    cid, user = update.effective_chat.id, update.effective_user
    if await is_admin(ctx, cid, user.id):
        return await update.message.reply_text("Admins can't use /kickme.")
    await ctx.bot.ban_chat_member(cid, user.id)
    await ctx.bot.unban_chat_member(cid, user.id)
    await ctx.bot.send_message(cid, f"👋 {user.full_name} has left the chat (/kickme).")


# ---------- punishments ----------
async def warn(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    t = await get_target(update, ctx)
    if not t:
        return
    cid = update.effective_chat.id
    q("INSERT OR IGNORE INTO warns VALUES(?,?,0)", cid, t.id)
    q("UPDATE warns SET n=n+1 WHERE chat_id=? AND user_id=?", cid, t.id)
    n = q("SELECT n FROM warns WHERE chat_id=? AND user_id=?", cid, t.id).fetchone()[0]
    reason = " ".join(ctx.args) or "No reason given"
    await update.message.reply_text(
        f"⚠️ {t.mention_html()} warned ({n}/{MAX_WARNS}).\nReason: {html.escape(reason)}",
        parse_mode="HTML")
    if n >= MAX_WARNS:
        await ctx.bot.ban_chat_member(cid, t.id)
        q("DELETE FROM warns WHERE chat_id=? AND user_id=?", cid, t.id)
        await update.message.reply_text(f"🔨 {t.full_name} banned after {MAX_WARNS} warnings.")


async def unwarn(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to a user with /unwarn.")
    q("UPDATE warns SET n=MAX(n-1,0) WHERE chat_id=? AND user_id=?",
      update.effective_chat.id, r.from_user.id)
    await update.message.reply_text("✅ One warning removed.")


async def warns(update: Update, ctx):
    r = update.message.reply_to_message
    user = r.from_user if r else update.effective_user
    row = q("SELECT n FROM warns WHERE chat_id=? AND user_id=?",
            update.effective_chat.id, user.id).fetchone()
    await update.message.reply_text(f"{user.full_name}: {row[0] if row else 0}/{MAX_WARNS} warnings.")


async def mute(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    t = await get_target(update, ctx)
    if not t:
        return
    mins = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 60
    await ctx.bot.restrict_chat_member(
        update.effective_chat.id, t.id, ChatPermissions(can_send_messages=False),
        until_date=update.message.date + timedelta(minutes=mins))
    await update.message.reply_text(f"🔇 {t.full_name} muted for {mins} min.")


async def unmute(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to a user with /unmute.")
    await ctx.bot.restrict_chat_member(update.effective_chat.id, r.from_user.id, FULL_PERMS)
    await update.message.reply_text(f"🔊 {r.from_user.full_name} unmuted.")


async def kick(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    t = await get_target(update, ctx)
    if not t:
        return
    cid = update.effective_chat.id
    await ctx.bot.ban_chat_member(cid, t.id)
    await ctx.bot.unban_chat_member(cid, t.id)
    await update.message.reply_text(f"👢 {t.full_name} removed.")


async def ban(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    t = await get_target(update, ctx)
    if not t:
        return
    await ctx.bot.ban_chat_member(update.effective_chat.id, t.id)
    await update.message.reply_text(f"🔨 {t.full_name} banned.")


async def unban(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Usage: /unban <user_id>")
    await ctx.bot.unban_chat_member(update.effective_chat.id, int(ctx.args[0]), only_if_banned=True)
    await update.message.reply_text("✅ Unbanned.")


async def delete_msg(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to the message you want deleted.")
    await r.delete()
    await update.message.delete()


async def pin(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to the message you want pinned.")
    await r.pin()


async def unpin(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    await ctx.bot.unpin_chat_message(update.effective_chat.id)


# ---------- /promote ----------
async def promote(update: Update, ctx):
    msg, chat = update.message, update.effective_chat
    if chat.type not in ("group", "supergroup"):
        return await msg.reply_text("Use this command in a group.")
    if not await admin_only(update, ctx):
        return
    # caller must be the owner or an admin with the "add new admins" right
    anonymous = msg.sender_chat and msg.sender_chat.id == chat.id
    if not anonymous:
        try:
            c = await ctx.bot.get_chat_member(chat.id, update.effective_user.id)
            allowed = c.status == ChatMemberStatus.OWNER or (
                c.status == ChatMemberStatus.ADMINISTRATOR and getattr(c, "can_promote_members", False))
        except Exception:
            allowed = False
        if not allowed:
            return await msg.reply_text("You need the 'Add new admins' permission to use /promote.")

    # find the target: reply, or @username / user_id argument
    target_id, target_name = None, None
    r = msg.reply_to_message
    if r and r.from_user and not (r.sender_chat and r.sender_chat.id == chat.id):
        target_id, target_name = r.from_user.id, r.from_user.full_name
    elif ctx.args:
        arg = ctx.args[0].lstrip("@")
        tm = next((e.user for e in (msg.entities or ()) if e.type == "text_mention" and e.user), None)
        if tm:
            target_id, target_name = tm.id, tm.full_name
        elif arg.isdigit():
            target_id = int(arg)
        else:
            # 1) this group's records, 2) any group the bot is in, 3) Telegram lookup
            row = (q("SELECT user_id, name FROM members WHERE chat_id=? AND username=?",
                     chat.id, arg.lower()).fetchone()
                   or q("SELECT user_id, name FROM members WHERE username=?", arg.lower()).fetchone())
            if row:
                target_id, target_name = row
            else:
                try:
                    target_id = (await ctx.bot.get_chat(f"@{arg}")).id
                except Exception:
                    try:
                        res = await tg_raw("getChat", chat_id=f"@{arg}")
                        target_id = res["id"]
                    except Exception:
                        found = await resolve_username(arg)
                        if found:
                            target_id, target_name = found
                        else:
                            return await msg.reply_text(
                                f"❌ Telegram doesn't let bots look up @{arg} unless the bot has seen them. "
                                "Reply to any of their messages with /promote, or use /promote <user_id>.")
    else:
        return await msg.reply_text("Reply to a user with /promote, or use /promote @username")

    try:
        member = await ctx.bot.get_chat_member(chat.id, target_id)
    except Exception:
        return await msg.reply_text("❌ I couldn't find that user in this group.")
    if member.status == ChatMemberStatus.OWNER:
        return await msg.reply_text("That's the group owner.")
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        return await msg.reply_text("They're already an admin.")
    if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return await msg.reply_text("❌ That user isn't in the group.")
    if member.user.is_bot:
        return await msg.reply_text("I won't promote bots.")
    target_name = target_name or member.user.full_name

    try:
        await ctx.bot.promote_chat_member(
            chat.id, target_id,
            can_manage_chat=True, can_delete_messages=True, can_restrict_members=True,
            can_pin_messages=True, can_invite_users=True, can_manage_video_chats=True,
            can_change_info=False, can_promote_members=False)
    except Exception as e:
        return await msg.reply_text(
            f"❌ Couldn't promote: {e}\nMake sure I'm an admin with the 'Add new admins' permission.")
    _admin_cache.pop((chat.id, target_id), None)
    await msg.reply_text(f"⭐ {html.escape(target_name)} is now an admin.", parse_mode="HTML")


# ---------- federation ban (/fban) ----------
async def fban(update: Update, ctx):
    if update.effective_user.id not in OWNER_IDS:
        return await update.message.reply_text("Only the bot owners can use /fban.")
    r = update.message.reply_to_message
    args = list(ctx.args)
    if r:
        uid, name = r.from_user.id, r.from_user.full_name
    elif args and args[0].lstrip("-").isdigit():
        uid, name = int(args.pop(0)), None
    else:
        return await update.message.reply_text("Reply to a user or use /fban <user_id> [reason].")
    if uid in OWNER_IDS:
        return await update.message.reply_text("I won't fban a bot owner.")
    reason = " ".join(args) or "No reason given"
    q("INSERT OR REPLACE INTO fbans VALUES(?,?)", uid, reason)
    chats = [c[0] for c in q("SELECT chat_id FROM chats").fetchall()]
    note = await update.message.reply_text(f"⏳ Federation-banning in {len(chats)} chats...")
    ok = failed = 0
    for cid in chats:
        try:
            if await is_admin(ctx, cid, uid):
                failed += 1
                continue
            await ctx.bot.ban_chat_member(cid, uid)
            ok += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.1)
    await note.edit_text(
        f"🔨 FBan: {name or uid} (<code>{uid}</code>)\nReason: {html.escape(reason)}\n"
        f"Banned in {ok} chat(s), failed in {failed}.\nThey'll also be auto-banned if they join any of my chats.",
        parse_mode="HTML")


async def unfban(update: Update, ctx):
    if update.effective_user.id not in OWNER_IDS:
        return await update.message.reply_text("Only the bot owners can use /unfban.")
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        return await update.message.reply_text("Usage: /unfban <user_id>")
    uid = int(ctx.args[0])
    q("DELETE FROM fbans WHERE user_id=?", uid)
    ok = 0
    for (cid,) in q("SELECT chat_id FROM chats").fetchall():
        try:
            await ctx.bot.unban_chat_member(cid, uid, only_if_banned=True)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.1)
    await update.message.reply_text(f"✅ Removed from federation bans (unbanned in {ok} chat(s)).")


# ---------- /tagall ----------
async def tagall(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    cid = update.effective_chat.id
    extra = html.escape(update.message.text.partition(" ")[2].strip())
    people = {}
    for uid, uname, name in q("SELECT user_id, username, name FROM members WHERE chat_id=?", cid):
        people[uid] = (uname, name)
    try:
        for a in await ctx.bot.get_chat_administrators(cid):
            if not a.user.is_bot:
                people[a.user.id] = ((a.user.username or "").lower(), a.user.full_name)
    except Exception:
        pass
    if not people:
        return await update.message.reply_text("I haven't seen any members yet.")
    mentions = [f"@{u}" if u else f'<a href="tg://user?id={uid}">{html.escape(n or "user")}</a>'
                for uid, (u, n) in people.items()]
    for i in range(0, len(mentions), TAGS_PER_MESSAGE):
        chunk = " ".join(mentions[i:i + TAGS_PER_MESSAGE])
        head = f"📣 {extra}\n" if (extra and i == 0) else ""
        await ctx.bot.send_message(cid, head + chunk, parse_mode="HTML")
        await asyncio.sleep(1.2)
    try:
        total = await ctx.bot.get_chat_member_count(cid)
        await ctx.bot.send_message(
            cid, f"Tagged {len(people)} of {total} members (Telegram only lets bots tag members they've seen).")
    except Exception:
        pass


# ---------- locks ----------
def parse_lock_types(args):
    types, unknown = [], []
    for tok in re.split(r"[,\s\[\]/|]+", " ".join(args).lower()):
        if not tok:
            continue
        if tok in LOCK_ALIASES:
            types += LOCK_ALIASES[tok]
        else:
            unknown.append(tok)
    return list(dict.fromkeys(types)), unknown


async def do_lock(update: Update, ctx, args, on: bool):
    if not await admin_only(update, ctx):
        return
    cid = update.effective_chat.id
    cmd = "lock" if on else "unlock"
    types, unknown = parse_lock_types(args)
    if not types:
        return await update.message.reply_text(
            f"Usage: /{cmd} all  or  /{cmd} messages stickers gifs photos videos voice audio "
            "documents polls forwards contacts location media links invites")
    locks = get_locks(cid)
    for t in types:
        if t == "all":
            await ctx.bot.set_chat_permissions(
                cid, NO_PERMS if on else FULL_PERMS, use_independent_chat_permissions=True)
            if on:
                locks.add("all")
            else:
                locks.clear()
        elif t == "links":
            put(cid, "links_ok", 0 if on else 1)
        elif on:
            locks.add(t)
        else:
            locks.discard(t)
    put_locks(cid, locks)
    msg = ("🔒 Locked: " if on else "🔓 Unlocked: ") + ", ".join(types)
    msg += ". Only admins can send messages now." if (on and "all" in types) else "."
    if unknown:
        msg += "\nUnknown: " + ", ".join(unknown)
    await update.message.reply_text(msg)


async def lock(update: Update, ctx):
    await do_lock(update, ctx, ctx.args, True)


async def unlock(update: Update, ctx):
    await do_lock(update, ctx, ctx.args, False)


async def lockforward(update: Update, ctx):
    await do_lock(update, ctx, ["forwards"], True)


async def unlockforward(update: Update, ctx):
    await do_lock(update, ctx, ["forwards"], False)


async def locks_cmd(update: Update, ctx):
    cid = update.effective_chat.id
    locks = get_locks(cid) - {"links"}
    if not get(cid, "links_ok"):
        locks.add("links")
    await update.message.reply_text("🔒 Active locks: " + (", ".join(sorted(locks)) or "none"))


async def lock_filter(update: Update, ctx):
    msg = update.effective_message
    if not msg or not msg.from_user:
        return
    locks = get_locks(msg.chat_id) - {"all", "links"}
    if not locks or not any(LOCK_CHECKS[t](msg) for t in locks if t in LOCK_CHECKS):
        return
    if await is_admin(ctx, msg.chat_id, msg.from_user.id):
        return
    try:
        await msg.delete()
    except Exception:
        pass


# ---------- /addtag ----------
TAG_LINE = re.compile(r"^\s*@?([A-Za-z0-9_]{3,32})\s*[-–—:]+\s*(.+?)\s*$")


async def addtag(update: Update, ctx):
    msg, chat, sender = update.message, update.effective_chat, update.effective_user
    lines = msg.text.splitlines()
    first = lines[0].split()[1:]
    gid = None
    if first and re.fullmatch(r"-?\d+", first[0]):
        gid, first = int(first[0]), first[1:]
    elif chat.type in ("group", "supergroup"):
        gid = chat.id
    entries = ([" ".join(first)] if first else []) + lines[1:]
    entries = [e for e in entries if e.strip()]
    if gid is None or not entries:
        return await msg.reply_text(
            "Usage:\n/addtag <group_id>\n@username - tag\n@username2 - tag2\n\n"
            "Tags: max 16 characters, no emoji. Each person must have sent a message in the "
            "group (or joined) while I was there so I can find them.")
    if not await is_admin(ctx, gid, sender.id):
        return await msg.reply_text("You must be an admin of that group (and I must be in it).")

    out = []
    for line in entries:
        m = TAG_LINE.match(line)
        if not m:
            out.append(f"❌ Can't read: {line}  (format: @username - tag)")
            continue
        uname, tag = m.group(1), m.group(2)
        if len(tag) > 16 or has_emoji(tag):
            out.append(f"❌ @{uname}: tag must be ≤16 characters, no emoji")
            continue
        row = q("SELECT user_id FROM members WHERE chat_id=? AND username=?", gid, uname.lower()).fetchone()
        uid = row[0] if row else None
        if uid is None:
            try:
                uid = (await ctx.bot.get_chat(f"@{uname}")).id
            except Exception:
                out.append(f"❌ @{uname}: I haven't seen this user yet (ask them to send a message in the group)")
                continue
        try:
            member = await ctx.bot.get_chat_member(gid, uid)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await ctx.bot.set_chat_administrator_custom_title(gid, uid, tag)
            elif member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
                await set_member_tag(ctx, gid, uid, tag)
            else:
                out.append(f"❌ @{uname}: not in the group")
                continue
            out.append(f"✅ @{uname} → {tag}")
        except Exception as e:
            out.append(f"❌ @{uname}: {e}")
    await msg.reply_text("\n".join(out))


# ---------- settings ----------
async def links(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    on = bool(ctx.args) and ctx.args[0].lower() == "on"
    put(update.effective_chat.id, "links_ok", int(on))
    await update.message.reply_text(f"Links from members: {'allowed' if on else 'blocked'}.")


async def flood_cmd(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    on = not (ctx.args and ctx.args[0].lower() == "off")
    put(update.effective_chat.id, "flood_on", int(on))
    await update.message.reply_text(f"Anti-flood: {'ON' if on else 'OFF'}.")


async def addword(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    w = " ".join(ctx.args).lower().strip()
    if not w:
        return await update.message.reply_text("Usage: /addword <word or phrase>")
    q("INSERT OR IGNORE INTO words VALUES(?,?)", update.effective_chat.id, w)
    await update.message.reply_text(f"✅ Blacklisted: {w}")


async def delword(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    w = " ".join(ctx.args).lower().strip()
    q("DELETE FROM words WHERE chat_id=? AND word=?", update.effective_chat.id, w)
    await update.message.reply_text("✅ Removed.")


async def words(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    rows = q("SELECT word FROM words WHERE chat_id=?", update.effective_chat.id).fetchall()
    await update.message.reply_text(("Blacklist:\n" + "\n".join(r[0] for r in rows)) if rows
                                    else "Blacklist is empty.")


# ---------- /settings panel ----------
SETTING_BUTTONS = [
    ("flood", "Anti-flood"), ("links", "Lock links"),
    ("invites", "Lock invite links"), ("forwards", "Lock forwards"),
    ("service", "Clean service msgs"), ("welcome", "Welcome"),
    ("goodbye", "Goodbye"),
]


def setting_state(cid, key) -> bool:
    if key == "flood":
        return bool(get(cid, "flood_on"))
    if key == "links":
        return not get(cid, "links_ok")
    if key in ("invites", "forwards"):
        return key in get_locks(cid)
    if key == "service":
        return bool(get(cid, "clean_service"))
    if key == "welcome":
        return bool(get(cid, "welcome_on"))
    return bool(get(cid, "goodbye_on"))


def setting_toggle(cid, key):
    new = not setting_state(cid, key)
    if key == "flood":
        put(cid, "flood_on", int(new))
    elif key == "links":
        put(cid, "links_ok", int(not new))
    elif key in ("invites", "forwards"):
        locks = get_locks(cid)
        (locks.add if new else locks.discard)(key)
        put_locks(cid, locks)
    elif key == "service":
        put(cid, "clean_service", int(new))
    elif key == "welcome":
        put(cid, "welcome_on", int(new))
    else:
        put(cid, "goodbye_on", int(new))


def settings_panel(cid):
    btns = [InlineKeyboardButton(f"{label}: {'✅' if setting_state(cid, key) else '❌'}",
                                 callback_data=f"set:{key}") for key, label in SETTING_BUTTONS]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("✖️ Close", callback_data="set:close")])
    text = ("⚙️ Moderation settings\nTap a button to toggle it. Details: "
            "/flood /lock /setwelcome /setgoodbye /welcome /goodbye")
    return text, InlineKeyboardMarkup(rows)


async def settings(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    text, kb = settings_panel(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def settings_cb(update: Update, ctx):
    qy = update.callback_query
    cid = qy.message.chat_id
    if not await is_admin(ctx, cid, qy.from_user.id):
        return await qy.answer("Admins only.", show_alert=True)
    key = qy.data.split(":", 1)[1]
    if key == "close":
        await qy.answer()
        return await qy.message.delete()
    if key in dict(SETTING_BUTTONS):
        setting_toggle(cid, key)
    await qy.edit_message_reply_markup(reply_markup=settings_panel(cid)[1])
    await qy.answer("Updated ✅")


async def clean_service(update: Update, ctx):
    msg = update.effective_message
    if msg and get(msg.chat_id, "clean_service"):
        try:
            await msg.delete()
        except Exception:
            pass


# ---------- /id and /info ----------
async def lookup_user(update: Update, ctx, arg: str):
    """Return dict(id, first, last, username) for a @username / numeric id, or None."""
    arg = arg.strip()
    if re.fullmatch(r"-?\d{3,}", arg):
        uid = int(arg)
        row = q("SELECT username, first_name, last_name FROM users WHERE user_id=?", uid).fetchone()
        if row:
            return dict(id=uid, username=row[0], first=row[1], last=row[2])
        try:
            c = await ctx.bot.get_chat(uid)
            return dict(id=uid, username=(c.username or "").lower(), first=c.first_name or "",
                        last=c.last_name or "")
        except Exception:
            row = q("SELECT name FROM members WHERE user_id=? LIMIT 1", uid).fetchone()
            return dict(id=uid, username="", first=row[0] if row else "", last="")
    uname = arg.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{3,32}", uname):
        return None
    row = q("SELECT user_id, first_name, last_name FROM users WHERE username=?", uname).fetchone()
    if row:
        return dict(id=row[0], username=uname, first=row[1], last=row[2])
    row = q("SELECT user_id, name FROM members WHERE username=? LIMIT 1", uname).fetchone()
    if row:
        return dict(id=row[0], username=uname, first=row[1], last="")
    res = await resolve_username(uname)
    if res:
        return dict(id=res[0], username=uname, first=res[1], last="")
    try:
        c = await ctx.bot.get_chat("@" + uname)
        if c.type == "private":
            return dict(id=c.id, username=uname, first=c.first_name or "", last=c.last_name or "")
    except Exception:
        pass
    return None


async def target_user(update: Update, ctx):
    """Who /id and /info are about: reply > argument > the sender. Returns (info, error)."""
    msg = update.message
    r = msg.reply_to_message
    if r and r.from_user and not ctx.args:
        u = r.from_user
        remember_user(u)
        return dict(id=u.id, username=(u.username or "").lower(), first=u.first_name or "",
                    last=u.last_name or ""), None
    if ctx.args:
        info = await lookup_user(update, ctx, ctx.args[0])
        if info:
            return info, None
        return None, ("I couldn't find that user. I only know people I've seen in a group I'm in — "
                      "try replying to their message, or use their numeric ID.")
    u = update.effective_user
    remember_user(u)
    return dict(id=u.id, username=(u.username or "").lower(), first=u.first_name or "",
                last=u.last_name or ""), None


def full(info) -> str:
    return " ".join(x for x in (info["first"], info["last"]) if x) or "User"


async def id_cmd(update: Update, ctx):
    msg, chat = update.message, update.effective_chat
    has_target = bool(ctx.args or msg.reply_to_message)
    info, err = await target_user(update, ctx)
    if err:
        return await msg.reply_text(err)
    if has_target:
        text = f"User <b>{html.escape(full(info))}</b>'s ID is <code>{info['id']}</code>."
    else:
        text = f"Your ID is <code>{info['id']}</code>."
    if chat.type != "private":
        text += f"\nThis chat's ID is <code>{chat.id}</code>."
    r = msg.reply_to_message
    if r and r.forward_origin and getattr(r.forward_origin, "sender_user", None) and not ctx.args:
        fu = r.forward_origin.sender_user
        text += f"\nThe forwarded user {html.escape(fu.full_name)}'s ID is <code>{fu.id}</code>."
    await msg.reply_text(text, parse_mode="HTML")


async def info_cmd(update: Update, ctx):
    msg = update.message
    info, err = await target_user(update, ctx)
    if err:
        return await msg.reply_text(err)
    lines = ["<b>User info:</b>", f"<b>ID:</b> <code>{info['id']}</code>"]
    if info["first"]:
        lines.append(f"<b>First Name:</b> {html.escape(info['first'])}")
    if info["last"]:
        lines.append(f"<b>Last Name:</b> {html.escape(info['last'])}")
    if info["username"]:
        lines.append(f"<b>Username:</b> @{info['username']}")
    lines.append(f'<b>User link:</b> <a href="tg://user?id={info["id"]}">link</a>')
    if update.effective_chat.type != "private":
        try:
            st = (await ctx.bot.get_chat_member(update.effective_chat.id, info["id"])).status
            lines.append(f"<b>Status here:</b> {html.escape(str(st))}")
        except Exception:
            pass
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


# ---------- /filter ----------
MEDIA_KINDS = ("photo", "video", "animation", "sticker", "document", "audio", "voice", "video_note")


def parse_keyword(raw: str):
    """'"hello there" reply text' -> ('hello there', 'reply text'); 'word rest' -> ('word', 'rest')."""
    raw = raw.strip()
    if raw[:1] in "\"'“":
        close = {'"': '"', "'": "'", "“": "”"}[raw[0]]
        end = raw.find(close, 1)
        if end > 0:
            return raw[1:end].strip().lower(), raw[end + 1:].strip()
    kw, _, rest = raw.partition(" ")
    return kw.strip().lower(), rest.strip()


def extract_saved(m):
    """(kind, file_id, body) from a message so it can be replayed later."""
    if m.text:
        return "text", None, HTML_MARK + m.text_html
    body = HTML_MARK + m.caption_html if m.caption else ""
    for kind in MEDIA_KINDS:
        obj = getattr(m, kind)
        if obj:
            fid = obj[-1].file_id if kind == "photo" else obj.file_id
            return kind, fid, body
    return None, None, None


async def filter_cmd(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    msg, cid = update.message, update.effective_chat.id
    raw = msg.text.partition(" ")[2]
    if not raw.strip():
        return await msg.reply_text(
            "Usage: reply to a message with /filter <word>\n"
            "or /filter <word> <reply text>\n"
            'For several words use quotes: /filter "good morning"')
    kw, rest = parse_keyword(raw)
    if not kw:
        return await msg.reply_text("Give me a word to filter.")
    r = msg.reply_to_message
    if r:
        kind, fid, body = extract_saved(r)
        if not kind:
            return await msg.reply_text("I can't save that kind of message as a filter.")
    elif rest:
        kind, fid, body = "text", None, HTML_MARK + html.escape(rest)
    else:
        return await msg.reply_text("Reply to a message, or add the reply text after the word.")
    q("INSERT OR REPLACE INTO filters VALUES(?,?,?,?,?)", cid, kw, kind, fid, body)
    rows = q("SELECT keyword FROM filters WHERE chat_id=? ORDER BY keyword", cid).fetchall()
    await msg.reply_text(f"Saved {len(rows)} filters in {update.effective_chat.title}:\n" +
                         "\n".join(f"- {r_[0]}" for r_ in rows))


async def filters_cmd(update: Update, ctx):
    rows = q("SELECT keyword FROM filters WHERE chat_id=? ORDER BY keyword",
             update.effective_chat.id).fetchall()
    await update.message.reply_text(
        ("Filters in this chat:\n" + "\n".join(f"- {r[0]}" for r in rows)) if rows
        else "No filters in this chat yet.")


async def stop_cmd(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    raw = update.message.text.partition(" ")[2]
    kw, _ = parse_keyword(raw) if raw.strip() else ("", "")
    if not kw:
        return await update.message.reply_text("Usage: /stop <word>")
    cur = q("DELETE FROM filters WHERE chat_id=? AND keyword=?", update.effective_chat.id, kw)
    await update.message.reply_text("✅ Filter removed." if cur.rowcount else "No such filter.")


async def stopall_cmd(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    q("DELETE FROM filters WHERE chat_id=?", update.effective_chat.id)
    await update.message.reply_text("✅ All filters removed.")


async def filter_trigger(update: Update, ctx):
    msg = update.message
    if not msg or not msg.from_user:
        return
    text = (msg.text or msg.caption or "").lower()
    if not text:
        return
    cid = msg.chat_id
    for kw, kind, fid, body in q("SELECT keyword, kind, file_id, body FROM filters WHERE chat_id=?",
                                 cid).fetchall():
        if not re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", text):
            continue
        caption = render(body, msg.from_user, update.effective_chat) if body else None
        rp = ReplyParameters(msg.message_id, allow_sending_without_reply=True)
        try:
            if kind == "text":
                await ctx.bot.send_message(cid, caption or "…", parse_mode="HTML", reply_parameters=rp)
            else:
                send = getattr(ctx.bot, "send_" + kind)
                kwargs = {"reply_parameters": rp}
                if kind not in ("sticker", "video_note"):
                    kwargs.update(caption=caption, parse_mode="HTML")
                await send(cid, fid, **kwargs)
        except Exception as e:
            logging.warning("filter send failed: %s", e)
        return  # one filter per message


# ---------- /q quotes ----------
FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF",
             "/usr/share/fonts/truetype/noto", "/usr/share/fonts/truetype/liberation",
             "/Library/Fonts", "C:/Windows/Fonts"]
FONT_FILES = {False: ["DejaVuSans.ttf", "NotoSans-Regular.ttf", "LiberationSans-Regular.ttf", "arial.ttf"],
              True: ["DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf"]}
NAME_COLORS = ["#ff885e", "#ffcd6a", "#82e68b", "#6ae0e8", "#6cb6ff", "#d89bff", "#ff8ec8"]


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    env = os.environ.get("QUOTE_FONT_BOLD" if bold else "QUOTE_FONT")
    cands = [env] if env else []
    cands += [os.path.join(d, f) for f in FONT_FILES[bold] for d in FONT_DIRS]
    for c in cands:
        if c and os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 built-in scalable font
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    lines = []
    for para in text.replace("\r", "").split("\n"):
        cur = ""
        for word in para.split(" "):
            trial = (cur + " " + word).strip() if cur else word
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
                continue
            if cur:
                lines.append(cur)
            while draw.textlength(word, font=font) > max_w and len(word) > 1:  # very long word
                k = len(word)
                while k > 1 and draw.textlength(word[:k], font=font) > max_w:
                    k -= 1
                lines.append(word[:k])
                word = word[k:]
            cur = word
        lines.append(cur)
    return lines


def _clean(t) -> str:
    """Turn fancy Unicode names (𝐌𝐒 𝐃𝐇𝐎𝐍𝐈, ＭＳ …) into plain letters and drop emoji the font can't draw."""
    t = unicodedata.normalize("NFKC", t or "")
    return "".join(c for c in t if unicodedata.category(c) not in ("So", "Cs", "Co", "Cn", "Cf")
                   and not 0xFE00 <= ord(c) <= 0xFE0F)


def render_quote(name: str, text: str, uid: int = 0, reply_name=None, reply_text=None,
                 avatar=None) -> bytes:
    """Draw a QuotLy-style quote sticker locally (WEBP, 512 px wide). Needs Pillow."""
    from PIL import Image, ImageDraw
    S = 2                                   # supersample for smooth edges
    W, MAXH = 512 * S, 512 * S
    pad, av = 16 * S, 56 * S
    bx0, bx1 = pad + av + 10 * S, W - pad
    inner = 16 * S
    tw = bx1 - bx0 - 2 * inner
    f_txt, f_name, f_small = _font(24 * S), _font(22 * S, True), _font(20 * S)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    lh, nh = int(24 * S * 1.3), int(22 * S * 1.35)
    name = _clean(name).strip() or "User"
    text = _clean(text).strip() or "…"
    reply_name, reply_text = _clean(reply_name), _clean(reply_text).strip()
    color = NAME_COLORS[abs(uid) % len(NAME_COLORS)]

    rq_lines = []
    if reply_text:
        rq_lines = _wrap(probe, reply_text, f_small, tw - 14 * S)[:2]
    rq_h = (int(20 * S * 1.3) * (len(rq_lines) + 1) + 10 * S) if rq_lines else 0

    lines = _wrap(probe, text, f_txt, tw)
    fixed = 2 * pad + 2 * 12 * S + nh + rq_h
    max_lines = max(1, (MAXH - fixed) // lh)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()[:-1] + "…"
    bh = 12 * S + nh + rq_h + lh * len(lines) + 12 * S
    H = max(bh, av) + 2 * pad

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((bx0, pad, bx1, pad + bh), radius=18 * S, fill="#1b1429")
    photo = None
    if avatar:
        try:
            photo = Image.open(io.BytesIO(avatar)).convert("RGBA").resize((av, av), Image.LANCZOS)
        except Exception:
            photo = None
    if photo:
        mask = Image.new("L", (av, av), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, av - 1, av - 1), fill=255)
        img.paste(photo, (pad, pad), mask)
    else:
        d.ellipse((pad, pad, pad + av, pad + av), fill=color)
        d.text((pad + av / 2, pad + av / 2), name[:1].upper(), font=_font(28 * S, True),
               fill="#1b1429", anchor="mm")
    y = pad + 12 * S
    d.text((bx0 + inner, y), name, font=f_name, fill=color)
    y += nh
    if rq_lines:
        d.rounded_rectangle((bx0 + inner, y, bx0 + inner + 3 * S, y + rq_h - 6 * S), radius=2 * S, fill=color)
        ry = y
        d.text((bx0 + inner + 12 * S, ry), reply_name or "", font=_font(20 * S, True), fill=color)
        ry += int(20 * S * 1.3)
        for ln in rq_lines:
            d.text((bx0 + inner + 12 * S, ry), ln, font=f_small, fill="#c9c3d6")
            ry += int(20 * S * 1.3)
        y += rq_h
    for ln in lines:
        d.text((bx0 + inner, y), ln, font=f_txt, fill="#ffffff")
        y += lh
    img = img.resize((W // S, H // S), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "WEBP", quality=95)
    return out.getvalue()


async def make_quote_png(target, with_reply: bool, bot=None):
    frm = target.sender_chat or target.from_user
    name = getattr(frm, "title", None) or frm.full_name
    rm = target.reply_to_message
    rname = rtext = None
    if with_reply and rm and (rm.text or rm.caption):
        rf = rm.sender_chat or rm.from_user
        rname = getattr(rf, "title", None) or (rf.full_name if rf else "")
        rtext = rm.text or rm.caption
    avatar = None
    if bot and target.from_user and not target.sender_chat:
        try:  # downloaded by the bot itself; the file URL (with the token) never leaves this server
            ph = await bot.get_user_profile_photos(target.from_user.id, limit=1)
            if ph.photos:
                f = await bot.get_file(ph.photos[0][0].file_id)
                avatar = bytes(await f.download_as_bytearray())
        except Exception as e:
            logging.info("no avatar for quote: %s", e)
    return await asyncio.to_thread(render_quote, name, target.text or target.caption, frm.id,
                                   rname, rtext, avatar)


async def quote_cmd(update: Update, ctx):
    msg = update.message
    target = msg.reply_to_message
    if not target:
        return await msg.reply_text("Reply to a message with /q to quote it. Use /q r to include the message it replied to.")
    if not (target.text or target.caption):
        return await msg.reply_text("I can only quote text messages (or captions).")
    if not (target.from_user or target.sender_chat):
        return await msg.reply_text("I can't quote that message.")
    with_reply = any(a.lower() in ("r", "reply") for a in ctx.args)
    tags = [a for a in ctx.args if a.startswith("@")]
    owner_id = target.from_user.id if target.from_user and not target.sender_chat else None
    if tags:  # /q @someone -> the quote is shown whenever @someone is tagged
        info = await lookup_user(update, ctx, tags[0])
        if not info:
            return await msg.reply_text("I don't know that @username yet — they must have talked in a group I'm in.")
        owner_id = info["id"]
    try:
        img = await make_quote_png(target, with_reply, ctx.bot)
    except Exception as e:
        logging.exception("quote failed")
        hint = " Install Pillow: pip install pillow" if isinstance(e, ImportError) else ""
        return await msg.reply_text(f"⚠️ Couldn't generate the quote: {type(e).__name__}.{hint}")
    f = io.BytesIO(img)
    f.name = "quote.webp"
    sent = await ctx.bot.send_sticker(msg.chat_id, f, reply_parameters=ReplyParameters(
        msg.message_id, allow_sending_without_reply=True))
    if update.effective_chat.type != "private" and sent.sticker:
        q("INSERT OR REPLACE INTO quotes VALUES(?,?,?)", msg.chat_id, target.message_id,
          sent.sticker.file_id)
        if owner_id:
            q("INSERT OR REPLACE INTO user_quotes VALUES(?,?,?)", msg.chat_id, owner_id,
              sent.sticker.file_id)


async def unquote_cmd(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    r = update.message.reply_to_message
    if not r:
        return await update.message.reply_text("Reply to the quoted message with /unq.")
    cid = update.effective_chat.id
    cur = q("DELETE FROM quotes WHERE chat_id=? AND msg_id=?", cid, r.message_id)
    n = cur.rowcount
    if r.from_user:
        n += q("DELETE FROM user_quotes WHERE chat_id=? AND user_id=?", cid, r.from_user.id).rowcount
    await update.message.reply_text("✅ Quote removed." if n else "No saved quote for that message or user.")


async def quote_trigger(update: Update, ctx):
    """Someone replied to a message that has a saved quote -> show the quote."""
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    row = q("SELECT file_id FROM quotes WHERE chat_id=? AND msg_id=?", msg.chat_id,
            msg.reply_to_message.message_id).fetchone()
    if row:
        try:
            await ctx.bot.send_sticker(msg.chat_id, row[0], reply_parameters=ReplyParameters(
                msg.message_id, allow_sending_without_reply=True))
        except Exception as e:
            logging.warning("quote send failed: %s", e)


_mention_cd = {}


async def mention_quote_trigger(update: Update, ctx):
    """Someone @tagged a user who has a saved quote -> show that quote."""
    msg = update.message
    if not msg or not msg.from_user:
        return
    ids = []
    for e in msg.entities or ():
        if e.type == "text_mention" and e.user:
            ids.append(e.user.id)
        elif e.type == "mention":
            uname = msg.text[e.offset + 1:e.offset + e.length].lower()
            row = q("SELECT user_id FROM users WHERE username=?", uname).fetchone()
            if row:
                ids.append(row[0])
    for uid in dict.fromkeys(ids):
        row = q("SELECT file_id FROM user_quotes WHERE chat_id=? AND user_id=?", msg.chat_id, uid).fetchone()
        if not row:
            continue
        key = (msg.chat_id, uid)
        if time.time() - _mention_cd.get(key, 0) < 20:  # anti-spam cooldown
            continue
        _mention_cd[key] = time.time()
        try:
            await ctx.bot.send_sticker(msg.chat_id, row[0], reply_parameters=ReplyParameters(
                msg.message_id, allow_sending_without_reply=True))
        except Exception as e:
            logging.warning("mention quote failed: %s", e)
        return


# ---------- automatic filters ----------
async def link_filter(update: Update, ctx):
    msg = update.message
    if not msg or get(msg.chat_id, "links_ok"):
        return
    if await is_admin(ctx, msg.chat_id, msg.from_user.id):
        return
    await msg.delete()
    await ctx.bot.send_message(msg.chat_id, f"🚫 {msg.from_user.mention_html()}, links aren't allowed here.",
                               parse_mode="HTML")


async def text_filter(update: Update, ctx):
    msg = update.message
    if not msg or not msg.from_user:
        return
    cid, uid = msg.chat_id, msg.from_user.id
    if await is_admin(ctx, cid, uid):
        return
    text = (msg.text or msg.caption or "").lower()
    for (w,) in q("SELECT word FROM words WHERE chat_id=?", cid).fetchall():
        if w in text:
            await msg.delete()
            await ctx.bot.send_message(cid, f"🚫 {msg.from_user.mention_html()}, that word isn't allowed.",
                                       parse_mode="HTML")
            return
    if get(cid, "flood_on"):
        dq = flood[(cid, uid)]
        now = time.time()
        dq.append(now)
        if len(dq) == FLOOD_MSGS and now - dq[0] <= FLOOD_SECS:
            dq.clear()
            await ctx.bot.restrict_chat_member(
                cid, uid, ChatPermissions(can_send_messages=False),
                until_date=msg.date + timedelta(minutes=FLOOD_MUTE_MIN))
            await ctx.bot.send_message(
                cid, f"🔇 {msg.from_user.mention_html()} muted for {FLOOD_MUTE_MIN} min (flooding).",
                parse_mode="HTML")


def main():
    app = Application.builder().token(TOKEN).build()
    cmds = {
        "start": start, "help": start, "rules": rules, "setrules": setrules,
        "setwelcome": setwelcome, "warn": warn, "unwarn": unwarn, "warns": warns,
        "mute": mute, "unmute": unmute, "kick": kick, "ban": ban, "unban": unban,
        "del": delete_msg, "pin": pin, "unpin": unpin, "links": links, "flood": flood_cmd,
        "addword": addword, "delword": delword, "words": words,
        "tagall": tagall, "lock": lock, "unlock": unlock, "locks": locks_cmd,
        "fban": fban, "unfban": unfban, "kickme": kickme, "addtag": addtag,
        "settings": settings, "welcome": welcome_cmd, "goodbye": goodbye_cmd,
        "id": id_cmd, "info": info_cmd, "filter": filter_cmd, "filters": filters_cmd,
        "stop": stop_cmd, "stopall": stopall_cmd, "q": quote_cmd, "unq": unquote_cmd,
        "promote": promote, "setgoodbye": setgoodbye, "lockforward": lockforward, "unlockforward": unlockforward,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    groups = filters.ChatType.GROUPS
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(chat_member_updates, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(join_request))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, goodbye))
    app.add_handler(CallbackQueryHandler(settings_cb, pattern=r"^set:"))
    app.add_handler(MessageHandler(groups & filters.ALL, tracker), group=-1)
    app.add_handler(MessageHandler(
        groups & (filters.Entity("url") | filters.Entity("text_link")), link_filter), group=1)
    app.add_handler(MessageHandler(groups & ~filters.COMMAND & (filters.TEXT | filters.CAPTION),
                                   text_filter), group=2)
    app.add_handler(MessageHandler(groups & filters.ALL, lock_filter), group=3)
    app.add_handler(MessageHandler(groups & filters.StatusUpdate.ALL, clean_service), group=4)
    app.add_handler(MessageHandler(groups & ~filters.COMMAND & (filters.TEXT | filters.CAPTION),
                                   filter_trigger), group=5)
    app.add_handler(MessageHandler(groups & filters.REPLY & ~filters.COMMAND, quote_trigger), group=6)
    app.add_handler(MessageHandler(
        groups & ~filters.COMMAND & (filters.Entity("mention") | filters.Entity("text_mention")),
        mention_quote_trigger), group=7)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
