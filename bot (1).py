import asyncio
import os
import re
import sqlite3

import httpx
from dotenv import load_dotenv
from pyrogram import Client, enums, filters, idle
from pyrogram.errors import FloodWait
from pytgcalls import PyTgCalls

import music

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]  # assistant account (for music)
OWNER_ID = int(os.environ["OWNER_ID"])

bot = Client("mgmt_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)
assistant = Client("assistant", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
call = PyTgCalls(assistant)

# ───────────────────────── Database ─────────────────────────
db = sqlite3.connect(os.getenv("DB_PATH", "bot.db"), check_same_thread=False)
db.executescript(
    """
CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, kind TEXT);
CREATE TABLE IF NOT EXISTS fbans(user_id INTEGER PRIMARY KEY, reason TEXT);
CREATE TABLE IF NOT EXISTS tags(chat_id INTEGER, username TEXT, tag TEXT,
                                applied INTEGER DEFAULT 0, PRIMARY KEY(chat_id, username));
CREATE TABLE IF NOT EXISTS locks(chat_id INTEGER, kind TEXT, PRIMARY KEY(chat_id, kind));
"""
)


def q(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur.fetchall()


# ───────────────────────── Helpers ─────────────────────────
ADMIN_STATUSES = (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER)
GROUPS = (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP)


async def is_admin(chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except Exception:
        return False


async def get_target(m, skip_args: int = 1):
    """Returns (user_id, reason). Target = reply, else first argument (id or @username)."""
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id, " ".join(m.command[1:])
    if len(m.command) > 1:
        arg = m.command[1]
        try:
            user = await bot.get_users(int(arg) if arg.lstrip("-").isdigit() else arg)
            return user.id, " ".join(m.command[2:])
        except Exception:
            return None, ""
    return None, ""


def register_chat(chat):
    kind = "group" if chat.type in GROUPS else "user"
    q("INSERT OR IGNORE INTO chats(chat_id, kind) VALUES(?, ?)", (chat.id, kind))


async def set_tag(chat_id: int, user_id: int, tag: str):
    """Bot API setChatMemberTag (bot needs the can_manage_tags admin right)."""
    async with httpx.AsyncClient(timeout=15) as h:
        r = await h.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMemberTag",
            params={"chat_id": chat_id, "user_id": user_id, "tag": tag},
        )
    j = r.json()
    return bool(j.get("ok")), j.get("description", "")


async def ban_everywhere(user_id: int):
    done = 0
    for (cid,) in q("SELECT chat_id FROM chats WHERE kind='group'"):
        try:
            await bot.ban_chat_member(cid, user_id)
            done += 1
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return done


# ───────────────────────── Start / Help ─────────────────────────
HELP = """💎 **Matrix Supreme** — Management + Music

**Moderation**
/ban – ban a user (admins only, can't ban admins)
/kickme – leave the group (non-admins)
/lock <all|message|stickers|gif> – lock for non-admins
/unlock <type|all> · /locks – manage locks

**Owner only**
/broadcast <text> (or reply) – send to all chats
/fban <user> [reason] · /unfban <user> – global ban
/addtag [groupid] + lines `username - Tag`

**Music** (premium player with buttons)
/play <name/link> · /pause · /resume · /skip · /end
/queue · /np · /loop · /shuffle · /seek <sec> · /volume <1-200>
/favorites – your saved songs"""


@bot.on_message(filters.command(["start", "help"]))
async def start(c, m):
    register_chat(m.chat)
    await m.reply(HELP)


# ───────────────────────── Moderation ─────────────────────────
@bot.on_message(filters.command("ban") & filters.group)
async def ban_cmd(c, m):
    if not m.from_user or not await is_admin(m.chat.id, m.from_user.id):
        return await m.reply("Only admins can use /ban.")
    uid, _ = await get_target(m)
    if not uid:
        return await m.reply("Reply to a user or give an ID/@username.")
    if uid == (await c.get_me()).id:
        return await m.reply("I can't ban myself.")
    if await is_admin(m.chat.id, uid):
        return await m.reply("I can't ban an admin.")
    try:
        await c.ban_chat_member(m.chat.id, uid)
        await m.reply(f"Banned `{uid}`.")
    except Exception as e:
        await m.reply(f"Failed: {e}")


@bot.on_message(filters.command("kickme") & filters.group)
async def kickme(c, m):
    if not m.from_user:
        return
    if await is_admin(m.chat.id, m.from_user.id):
        return await m.reply("Admins can't use /kickme.")
    try:
        await c.ban_chat_member(m.chat.id, m.from_user.id)
        await c.unban_chat_member(m.chat.id, m.from_user.id)  # kick = ban + unban
    except Exception as e:
        await m.reply(f"Failed: {e}")


@bot.on_message(filters.command("fban") & filters.user(OWNER_ID))
async def fban(c, m):
    uid, reason = await get_target(m)
    if not uid:
        return await m.reply("Reply to a user or give an ID/@username.")
    if uid == OWNER_ID:
        return await m.reply("I won't fban the owner.")
    q("INSERT OR REPLACE INTO fbans(user_id, reason) VALUES(?, ?)", (uid, reason or "-"))
    msg = await m.reply("Banning from all groups…")
    n = await ban_everywhere(uid)
    await msg.edit(f"Fbanned `{uid}` in {n} group(s). Auto-banned on any future join/message.")


@bot.on_message(filters.command("unfban") & filters.user(OWNER_ID))
async def unfban(c, m):
    uid, _ = await get_target(m)
    if not uid:
        return await m.reply("Reply to a user or give an ID/@username.")
    q("DELETE FROM fbans WHERE user_id=?", (uid,))
    done = 0
    for (cid,) in q("SELECT chat_id FROM chats WHERE kind='group'"):
        try:
            await c.unban_chat_member(cid, uid)
            done += 1
        except Exception:
            pass
        await asyncio.sleep(0.3)
    await m.reply(f"Un-fbanned `{uid}` in {done} group(s).")


# ───────────────────────── Locks ─────────────────────────
LOCK_TYPES = {"all", "message", "stickers", "gif"}
ALIASES = {"sticker": "stickers", "messages": "message", "text": "message", "gifs": "gif"}


def parse_lock(m):
    if len(m.command) < 2:
        return None
    t = m.command[1].lower()
    t = ALIASES.get(t, t)
    return t if t in LOCK_TYPES else None


@bot.on_message(filters.command("lock") & filters.group)
async def lock_cmd(c, m):
    if not m.from_user or not await is_admin(m.chat.id, m.from_user.id):
        return await m.reply("Only admins can use /lock.")
    t = parse_lock(m)
    if not t:
        return await m.reply("Usage: /lock <all|message|stickers|gif>")
    q("INSERT OR IGNORE INTO locks VALUES(?, ?)", (m.chat.id, t))
    await m.reply(f"Locked **{t}** for non-admins.")


@bot.on_message(filters.command("unlock") & filters.group)
async def unlock_cmd(c, m):
    if not m.from_user or not await is_admin(m.chat.id, m.from_user.id):
        return await m.reply("Only admins can use /unlock.")
    t = parse_lock(m)
    if not t:
        return await m.reply("Usage: /unlock <all|message|stickers|gif>")
    if t == "all":
        q("DELETE FROM locks WHERE chat_id=?", (m.chat.id,))
    else:
        q("DELETE FROM locks WHERE chat_id=? AND kind=?", (m.chat.id, t))
    await m.reply(f"Unlocked **{t}**.")


@bot.on_message(filters.command("locks") & filters.group)
async def locks_cmd(c, m):
    kinds = [r[0] for r in q("SELECT kind FROM locks WHERE chat_id=?", (m.chat.id,))]
    await m.reply("Active locks: " + (", ".join(kinds) if kinds else "none"))


# ───────────────────────── Tags ─────────────────────────
@bot.on_message(filters.command("addtag") & filters.user(OWNER_ID))
async def addtag(c, m):
    lines = m.text.strip().splitlines()
    head = lines[0].split()
    if len(head) > 1:
        try:
            chat_id = int(head[1])
        except ValueError:
            return await m.reply("Group ID must be a number, e.g. -1001234567890")
    elif m.chat.type in GROUPS:
        chat_id = m.chat.id
    else:
        return await m.reply("Usage:\n/addtag <groupid>\nusername - Tag\nusername - Tag")

    report, saved = [], 0
    for line in lines[1:]:
        mt = re.match(r"^\s*@?(\w+)\s*-\s*(.+?)\s*$", line)
        if not mt:
            continue
        username, tag = mt.group(1).lower(), mt.group(2)
        if len(tag) > 16:
            report.append(f"✗ {username}: tag longer than 16 chars")
            continue
        q("INSERT OR REPLACE INTO tags VALUES(?, ?, ?, 0)", (chat_id, username, tag))
        saved += 1
        # try to apply right away; otherwise it's applied when the user next speaks/joins
        try:
            user = await c.get_users(username)
            ok, desc = await set_tag(chat_id, user.id, tag)
            if ok:
                q("UPDATE tags SET applied=1 WHERE chat_id=? AND username=?", (chat_id, username))
                report.append(f"✓ {username} → {tag}")
            else:
                report.append(f"… {username}: saved, not applied yet ({desc})")
        except Exception:
            report.append(f"… {username}: saved, will apply when they join/chat")
    await m.reply(f"Saved {saved} tag(s) for `{chat_id}`.\n" + "\n".join(report))


async def apply_pending_tag(chat_id: int, user):
    if not user or not user.username:
        return
    row = q(
        "SELECT tag FROM tags WHERE chat_id=? AND username=? AND applied=0",
        (chat_id, user.username.lower()),
    )
    if row:
        ok, _ = await set_tag(chat_id, user.id, row[0][0])
        if ok:
            q(
                "UPDATE tags SET applied=1 WHERE chat_id=? AND username=?",
                (chat_id, user.username.lower()),
            )


# ───────────────────────── Group watcher ─────────────────────────
@bot.on_message(filters.group & ~filters.service, group=-1)
async def watcher(c, m):
    register_chat(m.chat)
    if not m.from_user:
        return
    uid = m.from_user.id

    # global ban
    if q("SELECT 1 FROM fbans WHERE user_id=?", (uid,)):
        try:
            await c.ban_chat_member(m.chat.id, uid)
            await m.delete()
        except Exception:
            pass
        return m.stop_propagation()

    await apply_pending_tag(m.chat.id, m.from_user)

    # locks
    kinds = {r[0] for r in q("SELECT kind FROM locks WHERE chat_id=?", (m.chat.id,))}
    if kinds:
        hit = (
            "all" in kinds
            or ("stickers" in kinds and m.sticker)
            or ("gif" in kinds and m.animation)
            or ("message" in kinds and m.text)
        )
        if hit and not await is_admin(m.chat.id, uid):
            try:
                await m.delete()
            except Exception:
                pass
            return m.stop_propagation()


@bot.on_message(filters.new_chat_members)
async def on_join(c, m):
    register_chat(m.chat)
    for user in m.new_chat_members:
        if user.is_self:
            continue
        if q("SELECT 1 FROM fbans WHERE user_id=?", (user.id,)):
            try:
                await c.ban_chat_member(m.chat.id, user.id)
            except Exception:
                pass
            continue
        await apply_pending_tag(m.chat.id, user)


@bot.on_chat_member_updated()
async def on_my_status(c, u):
    member = u.new_chat_member or u.old_chat_member
    if not member or not member.user.is_self:
        return
    gone = (not u.new_chat_member) or u.new_chat_member.status in (
        enums.ChatMemberStatus.LEFT,
        enums.ChatMemberStatus.BANNED,
    )
    if gone:
        q("DELETE FROM chats WHERE chat_id=?", (u.chat.id,))
    else:
        register_chat(u.chat)


# ───────────────────────── Broadcast ─────────────────────────
@bot.on_message(filters.command("broadcast") & filters.user(OWNER_ID))
async def broadcast(c, m):
    if not m.reply_to_message and len(m.command) < 2:
        return await m.reply("Usage: /broadcast <text>  (or reply to a message)")
    ids = [r[0] for r in q("SELECT chat_id FROM chats")]
    status = await m.reply(f"Broadcasting to {len(ids)} chats…")
    ok = fail = 0
    for cid in ids:
        for attempt in range(2):
            try:
                if m.reply_to_message:
                    await m.reply_to_message.copy(cid)
                else:
                    await c.send_message(cid, m.text.split(None, 1)[1])
                ok += 1
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                fail += 1
                break
        else:
            fail += 1
        await asyncio.sleep(0.1)
    await status.edit(f"Broadcast done. ✅ {ok} · ❌ {fail}")


# ───────────────────────── Run ─────────────────────────
music.setup(bot, assistant, call, is_admin, q)


async def main():
    await bot.start()
    await assistant.start()
    await call.start()
    print("Bot is running.")
    await idle()
    await bot.stop()
    await assistant.stop()


if __name__ == "__main__":
    asyncio.run(main())
