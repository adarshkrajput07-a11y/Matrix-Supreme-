"""
Matrix Supreme - Telegram Group Moderation Bot
Requires: pip install "python-telegram-bot>=21"
Run:
    BOT_TOKEN=123:ABC OWNER_IDS=111111,222222 python matrix_supreme_bot.py
Optional: DB_PATH=/data/matrix_supreme.db (use a persistent volume on Railway)

OWNER_IDS = Telegram user IDs allowed to use /fban and /unfban (global ban).
Make the bot an ADMIN in your group with: delete messages, ban users, pin messages,
and "manage tags" (for /addtag). In @BotFather: /setprivacy -> Disable.
"""
import asyncio
import html
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

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, MessageHandler, filters)

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
    "/tagall [message]\n\n"
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


async def set_member_tag(ctx, chat_id, user_id, tag):
    if hasattr(ctx.bot, "set_chat_member_tag"):
        return await ctx.bot.set_chat_member_tag(chat_id, user_id, tag)
    return await tg_raw("setChatMemberTag", chat_id=chat_id, user_id=user_id, tag=tag)


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
    for u in users:
        if u.is_bot:
            continue
        q("INSERT OR REPLACE INTO members VALUES(?,?,?,?)", chat.id, u.id,
          (u.username or "").lower(), u.full_name)
        if q("SELECT 1 FROM fbans WHERE user_id=?", u.id).fetchone():
            if not await is_admin(ctx, chat.id, u.id):
                try:
                    await ctx.bot.ban_chat_member(chat.id, u.id)
                    await ctx.bot.send_message(chat.id, f"🔨 {u.full_name} is federation-banned and was removed.")
                except Exception:
                    pass


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


async def setwelcome(update: Update, ctx):
    if not await admin_only(update, ctx):
        return
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        return await update.message.reply_text("Usage: /setwelcome Welcome {name} ({username}) to {group}! Read /rules.\n"
            "Placeholders: {name} = member's name, {username} = their @username, {group} = group name")
    put(update.effective_chat.id, "welcome", text)
    await update.message.reply_text("✅ Welcome message updated.")


def render(template: str, user, chat) -> str:
    """Fill {name}, {username}, {group} in welcome/goodbye templates."""
    uname = f"@{user.username}" if user.username else html.escape(user.full_name)
    return (html.escape(template)
            .replace("{name}", user.mention_html())
            .replace("{username}", uname)
            .replace("{group}", html.escape(chat.title or "the group")))


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
    text = update.message.text.partition(" ")[2].strip()
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
        "setgoodbye": setgoodbye, "lockforward": lockforward, "unlockforward": unlockforward,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    groups = filters.ChatType.GROUPS
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
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
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
