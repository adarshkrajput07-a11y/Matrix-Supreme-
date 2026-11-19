"""Extra group-management commands for Matrix Supreme."""
import asyncio
import html
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from pyrogram import enums, filters
from pyrogram.types import ChatPermissions, ChatPrivileges
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M

HTML = enums.ParseMode.HTML
LINK_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.(me|dog)/)", re.I)
RIGHT_NAMES = {
    "can_delete_messages": "delete messages",
    "can_restrict_members": "ban users",
    "can_pin_messages": "pin messages",
    "can_promote_members": "add admins",
}


def parse_dur(s: str):
    mt = re.fullmatch(r"(\d+)([smhdw])", (s or "").lower())
    if not mt:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[mt[2]]
    return timedelta(seconds=int(mt[1]) * mult)


def setup(bot, is_admin, q, get_target, OWNER_ID):
    q("CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(chat_id, key))")
    q("CREATE TABLE IF NOT EXISTS warns(chat_id INTEGER, user_id INTEGER, count INTEGER, PRIMARY KEY(chat_id, user_id))")
    q("CREATE TABLE IF NOT EXISTS blacklist(chat_id INTEGER, word TEXT, PRIMARY KEY(chat_id, word))")

    cache: dict[int, dict] = {}
    bl_cache: dict[int, set] = {}
    flood: dict[tuple, deque] = defaultdict(deque)
    me_id: list[int] = []

    # ───────── helpers ─────────
    def cfg(chat_id: int) -> dict:
        if chat_id not in cache:
            cache[chat_id] = {k: v for k, v in q("SELECT key, value FROM settings WHERE chat_id=?", (chat_id,))}
        return cache[chat_id]

    def put(chat_id: int, key: str, val):
        q("INSERT OR REPLACE INTO settings VALUES(?, ?, ?)", (chat_id, key, str(val)))
        cfg(chat_id)[key] = str(val)

    def drop(chat_id: int, key: str):
        q("DELETE FROM settings WHERE chat_id=? AND key=?", (chat_id, key))
        cfg(chat_id).pop(key, None)

    def words(chat_id: int) -> set:
        if chat_id not in bl_cache:
            bl_cache[chat_id] = {r[0] for r in q("SELECT word FROM blacklist WHERE chat_id=?", (chat_id,))}
        return bl_cache[chat_id]

    async def my_id() -> int:
        if not me_id:
            me_id.append((await bot.get_me()).id)
        return me_id[0]

    async def has_right(chat_id: int, uid: int, right: str | None = None) -> bool:
        if uid == OWNER_ID:
            return True
        try:
            mem = await bot.get_chat_member(chat_id, uid)
        except Exception:
            return False
        if mem.status == enums.ChatMemberStatus.OWNER:
            return True
        if mem.status != enums.ChatMemberStatus.ADMINISTRATOR:
            return False
        return True if right is None else bool(mem.privileges and getattr(mem.privileges, right, False))

    async def need(m, right: str | None = None) -> bool:
        if m.from_user and await has_right(m.chat.id, m.from_user.id, right):
            return True
        extra = f" ({RIGHT_NAMES[right]})" if right in RIGHT_NAMES else ""
        await m.reply(f"You need admin rights{extra} for that.")
        return False

    async def target(m, protect: bool = True):
        uid, rest = await get_target(m)
        if not uid:
            await m.reply("Reply to a user or give an ID/@username.")
            return None, ""
        if uid == await my_id():
            await m.reply("I can't do that to myself.")
            return None, ""
        if protect and await is_admin(m.chat.id, uid):
            await m.reply("I can't do that to an admin.")
            return None, ""
        return uid, rest

    async def name_of(uid: int) -> str:
        try:
            u = await bot.get_users(uid)
            return f'<a href="tg://user?id={uid}">{html.escape(u.first_name or str(uid))}</a>'
        except Exception:
            return f"<code>{uid}</code>"

    async def temp(m, text: str, secs: int = 6):
        msg = await bot.send_message(m.chat.id, text, parse_mode=HTML)
        await asyncio.sleep(secs)
        try:
            await msg.delete()
        except Exception:
            pass

    async def fail(m, e):
        await m.reply(f"❌ Failed: {e}")

    # ───────── purge / delete ─────────
    @bot.on_message(filters.command("purge") & filters.group)
    async def purge(c, m):
        if not await need(m, "can_delete_messages"):
            return
        if not m.reply_to_message:
            return await m.reply("Reply to the message you want to purge FROM.")
        ids = list(range(m.reply_to_message.id, m.id + 1))[:2000]
        try:
            for i in range(0, len(ids), 100):
                await c.delete_messages(m.chat.id, ids[i:i + 100])
            await temp(m, f"🧹 Purged <b>{len(ids)}</b> messages.", 4)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("del") & filters.group)
    async def delete_one(c, m):
        if not await need(m, "can_delete_messages") or not m.reply_to_message:
            return
        try:
            await c.delete_messages(m.chat.id, [m.reply_to_message.id, m.id])
        except Exception as e:
            await fail(m, e)

    # ───────── promote / demote / title ─────────
    @bot.on_message(filters.command(["promote", "fullpromote"]) & filters.group)
    async def promote(c, m):
        if not await need(m, "can_promote_members"):
            return
        uid, title = await target(m, protect=False)
        if not uid:
            return
        full = m.command[0].lower() == "fullpromote"
        rights = ChatPrivileges(
            can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
            can_restrict_members=True, can_invite_users=True, can_pin_messages=True,
            can_change_info=full, can_promote_members=full,
        )
        try:
            await c.promote_chat_member(m.chat.id, uid, rights)
            if title.strip():
                try:
                    await c.set_administrator_title(m.chat.id, uid, title.strip()[:16])
                except Exception:
                    pass
            await m.reply(f"⬆️ Promoted {await name_of(uid)}" + (f" as <b>{html.escape(title.strip()[:16])}</b>" if title.strip() else "") + ".", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("demote") & filters.group)
    async def demote(c, m):
        if not await need(m, "can_promote_members"):
            return
        uid, _ = await target(m, protect=False)
        if not uid:
            return
        none = ChatPrivileges(
            can_manage_chat=False, can_delete_messages=False, can_manage_video_chats=False,
            can_restrict_members=False, can_invite_users=False, can_pin_messages=False,
            can_change_info=False, can_promote_members=False,
        )
        try:
            await c.promote_chat_member(m.chat.id, uid, none)
            await m.reply(f"⬇️ Demoted {await name_of(uid)}.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("title") & filters.group)
    async def title_cmd(c, m):
        if not await need(m, "can_promote_members"):
            return
        uid, title = await target(m, protect=False)
        if not uid or not title.strip():
            return await m.reply("Usage: /title <user> <title>  (max 16 chars; only for admins I promoted)")
        try:
            await c.set_administrator_title(m.chat.id, uid, title.strip()[:16])
            await m.reply("✅ Title set.")
        except Exception as e:
            await fail(m, e)

    # ───────── kick / unban / mute / tmute / tban ─────────
    @bot.on_message(filters.command("kick") & filters.group)
    async def kick(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, _ = await target(m)
        if not uid:
            return
        try:
            await c.ban_chat_member(m.chat.id, uid)
            await c.unban_chat_member(m.chat.id, uid)
            await m.reply(f"👢 Kicked {await name_of(uid)}.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("unban") & filters.group)
    async def unban(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, _ = await target(m, protect=False)
        if not uid:
            return
        try:
            await c.unban_chat_member(m.chat.id, uid)
            await m.reply(f"✅ Unbanned {await name_of(uid)}.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("tban") & filters.group)
    async def tban(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, rest = await target(m)
        dur = parse_dur(rest.split()[0]) if rest.split() else None
        if not uid or not dur:
            return await m.reply("Usage: /tban <user> <10m|2h|1d|1w>")
        try:
            await c.ban_chat_member(m.chat.id, uid, until_date=datetime.now() + dur)
            await m.reply(f"⏳ Banned {await name_of(uid)} for <b>{rest.split()[0]}</b>.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("mute") & filters.group)
    async def mute(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, _ = await target(m)
        if not uid:
            return
        try:
            await c.restrict_chat_member(m.chat.id, uid, ChatPermissions())
            await m.reply(f"🔇 Muted {await name_of(uid)}.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("tmute") & filters.group)
    async def tmute(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, rest = await target(m)
        dur = parse_dur(rest.split()[0]) if rest.split() else None
        if not uid or not dur:
            return await m.reply("Usage: /tmute <user> <10m|2h|1d|1w>")
        try:
            await c.restrict_chat_member(m.chat.id, uid, ChatPermissions(), until_date=datetime.now() + dur)
            await m.reply(f"🔇 Muted {await name_of(uid)} for <b>{rest.split()[0]}</b>.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("unmute") & filters.group)
    async def unmute(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, _ = await target(m, protect=False)
        if not uid:
            return
        try:
            chat = await c.get_chat(m.chat.id)
            await c.restrict_chat_member(m.chat.id, uid, chat.permissions)
            await m.reply(f"🔊 Unmuted {await name_of(uid)}.", parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    # ───────── warns ─────────
    @bot.on_message(filters.command("warn") & filters.group)
    async def warn(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, reason = await target(m)
        if not uid:
            return
        row = q("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
        n = (row[0][0] if row else 0) + 1
        limit = int(cfg(m.chat.id).get("warn_limit", 3))
        who = await name_of(uid)
        if n >= limit:
            q("DELETE FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
            try:
                await c.ban_chat_member(m.chat.id, uid)
                return await m.reply(f"🚫 {who} reached <b>{limit}/{limit}</b> warns and was banned.", parse_mode=HTML)
            except Exception as e:
                return await fail(m, e)
        q("INSERT OR REPLACE INTO warns VALUES(?, ?, ?)", (m.chat.id, uid, n))
        text = f"⚠️ Warned {who}  (<b>{n}/{limit}</b>)" + (f"\nReason: {html.escape(reason)}" if reason else "")
        await m.reply(text, parse_mode=HTML, reply_markup=M([[B("🗑 Remove warn", callback_data=f"rw:{uid}")]]))

    @bot.on_callback_query(filters.regex(r"^rw:"))
    async def remove_warn_cb(c, cq):
        chat_id = cq.message.chat.id
        if not await has_right(chat_id, cq.from_user.id, "can_restrict_members"):
            return await cq.answer("Admins only.", show_alert=True)
        uid = int(cq.data.split(":")[1])
        row = q("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
        n = (row[0][0] if row else 0) - 1
        if n > 0:
            q("INSERT OR REPLACE INTO warns VALUES(?, ?, ?)", (chat_id, uid, n))
        else:
            q("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid))
        await cq.message.edit_text("✅ Warn removed by an admin.")
        await cq.answer()

    @bot.on_message(filters.command("warns") & filters.group)
    async def warns(c, m):
        uid, _ = await get_target(m)
        uid = uid or (m.from_user.id if m.from_user else None)
        row = q("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
        limit = cfg(m.chat.id).get("warn_limit", 3)
        await m.reply(f"{await name_of(uid)} has <b>{row[0][0] if row else 0}/{limit}</b> warns.", parse_mode=HTML)

    @bot.on_message(filters.command(["resetwarns", "unwarn"]) & filters.group)
    async def reset_warns(c, m):
        if not await need(m, "can_restrict_members"):
            return
        uid, _ = await get_target(m)
        if not uid:
            return await m.reply("Reply to a user or give an ID/@username.")
        q("DELETE FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
        await m.reply("✅ Warns reset.")

    @bot.on_message(filters.command("setwarnlimit") & filters.group)
    async def set_warn_limit(c, m):
        if not await need(m):
            return
        try:
            n = max(2, min(20, int(m.command[1])))
        except (IndexError, ValueError):
            return await m.reply("Usage: /setwarnlimit <2-20>")
        put(m.chat.id, "warn_limit", n)
        await m.reply(f"✅ Warn limit set to {n}.")

    # ───────── pins ─────────
    @bot.on_message(filters.command("pin") & filters.group)
    async def pin(c, m):
        if not await need(m, "can_pin_messages") or not m.reply_to_message:
            return
        loud = len(m.command) > 1 and m.command[1].lower() == "loud"
        try:
            await c.pin_chat_message(m.chat.id, m.reply_to_message.id, disable_notification=not loud)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("unpin") & filters.group)
    async def unpin(c, m):
        if not await need(m, "can_pin_messages"):
            return
        try:
            await c.unpin_chat_message(m.chat.id, m.reply_to_message.id if m.reply_to_message else 0)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("unpinall") & filters.group)
    async def unpin_all(c, m):
        if not await need(m, "can_pin_messages"):
            return
        try:
            await c.unpin_all_chat_messages(m.chat.id)
            await m.reply("📌 All messages unpinned.")
        except Exception as e:
            await fail(m, e)

    # ───────── group settings ─────────
    @bot.on_message(filters.command("slowmode") & filters.group)
    async def slowmode(c, m):
        if not await need(m):
            return
        try:
            secs = int(m.command[1])
            await c.set_slow_mode(m.chat.id, secs)
            await m.reply(f"🐢 Slow mode: {secs}s" if secs else "Slow mode off.")
        except (IndexError, ValueError):
            await m.reply("Usage: /slowmode <0|10|30|60|300|900|3600>")
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("setgtitle") & filters.group)
    async def set_title(c, m):
        if not await need(m) or len(m.command) < 2:
            return
        try:
            await c.set_chat_title(m.chat.id, m.text.split(None, 1)[1])
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("setdesc") & filters.group)
    async def set_desc(c, m):
        if not await need(m) or len(m.command) < 2:
            return
        try:
            await c.set_chat_description(m.chat.id, m.text.split(None, 1)[1])
            await m.reply("✅ Description updated.")
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("invitelink") & filters.group)
    async def invitelink(c, m):
        if not await need(m):
            return
        try:
            await m.reply(await c.export_chat_invite_link(m.chat.id))
        except Exception as e:
            await fail(m, e)

    # ───────── rules & welcome ─────────
    @bot.on_message(filters.command("setrules") & filters.group)
    async def set_rules(c, m):
        if not await need(m) or len(m.command) < 2:
            return await m.reply("Usage: /setrules <text>")
        put(m.chat.id, "rules", m.text.split(None, 1)[1])
        await m.reply("✅ Rules saved. Members can use /rules.")

    @bot.on_message(filters.command("rules") & filters.group)
    async def rules(c, m):
        text = cfg(m.chat.id).get("rules")
        await m.reply(f"📜 <b>Rules</b>\n\n{html.escape(text)}" if text else "No rules set yet.", parse_mode=HTML)

    @bot.on_message(filters.command("clearrules") & filters.group)
    async def clear_rules(c, m):
        if await need(m):
            drop(m.chat.id, "rules")
            await m.reply("✅ Rules cleared.")

    @bot.on_message(filters.command("welcome") & filters.group)
    async def welcome_toggle(c, m):
        if not await need(m):
            return
        arg = m.command[1].lower() if len(m.command) > 1 else ""
        if arg in ("on", "off"):
            put(m.chat.id, "welcome_on", "1" if arg == "on" else "0")
        state = "ON" if cfg(m.chat.id).get("welcome_on") == "1" else "OFF"
        await m.reply(f"Welcome messages are <b>{state}</b>.\nUse /welcome on|off, /setwelcome <text>.\n"
                      "Placeholders: {mention} {first} {group} {id}", parse_mode=HTML)

    @bot.on_message(filters.command("setwelcome") & filters.group)
    async def set_welcome(c, m):
        if not await need(m) or len(m.command) < 2:
            return await m.reply("Usage: /setwelcome Welcome {mention} to {group}!")
        put(m.chat.id, "welcome_text", m.text.split(None, 1)[1])
        put(m.chat.id, "welcome_on", "1")
        await m.reply("✅ Welcome message saved and turned on.")

    @bot.on_message(filters.command("resetwelcome") & filters.group)
    async def reset_welcome(c, m):
        if await need(m):
            drop(m.chat.id, "welcome_text")
            await m.reply("✅ Welcome message reset to default.")

    @bot.on_message(filters.new_chat_members, group=1)
    async def welcome(c, m):
        cf = cfg(m.chat.id)
        if cf.get("welcome_on") != "1":
            return
        tpl = html.escape(cf.get("welcome_text") or "Welcome {mention} to {group}! 🎉")
        for u in m.new_chat_members:
            if u.is_self:
                continue
            name = html.escape(u.first_name or "friend")
            text = (tpl.replace("{mention}", f'<a href="tg://user?id={u.id}">{name}</a>')
                    .replace("{first}", name).replace("{id}", str(u.id))
                    .replace("{group}", html.escape(m.chat.title or "")))
            try:
                await m.reply(text, parse_mode=HTML)
            except Exception:
                pass

    # ───────── protection: antiflood / antilink / blacklist ─────────
    @bot.on_message(filters.command("setflood") & filters.group)
    async def set_flood(c, m):
        if not await need(m):
            return
        arg = m.command[1].lower() if len(m.command) > 1 else ""
        if arg in ("0", "off"):
            drop(m.chat.id, "flood")
            return await m.reply("Anti-flood off.")
        try:
            n = max(3, min(30, int(arg)))
        except ValueError:
            return await m.reply("Usage: /setflood <3-30 messages in 6 sec> or /setflood off")
        put(m.chat.id, "flood", n)
        await m.reply(f"🌊 Anti-flood on: more than {n} messages in 6s = 10 min mute.")

    @bot.on_message(filters.command("antilink") & filters.group)
    async def antilink(c, m):
        if not await need(m):
            return
        arg = m.command[1].lower() if len(m.command) > 1 else ""
        if arg in ("on", "off"):
            put(m.chat.id, "antilink", "1" if arg == "on" else "0")
        state = "ON" if cfg(m.chat.id).get("antilink") == "1" else "OFF"
        await m.reply(f"🔗 Anti-link is <b>{state}</b>. Use /antilink on|off.", parse_mode=HTML)

    @bot.on_message(filters.command("addblacklist") & filters.group)
    async def add_bl(c, m):
        if not await need(m) or len(m.command) < 2:
            return await m.reply("Usage: /addblacklist <word or phrase>")
        w = m.text.split(None, 1)[1].lower().strip()
        q("INSERT OR IGNORE INTO blacklist VALUES(?, ?)", (m.chat.id, w))
        words(m.chat.id).add(w)
        await m.reply(f"🚫 Blacklisted: <code>{html.escape(w)}</code>", parse_mode=HTML)

    @bot.on_message(filters.command("rmblacklist") & filters.group)
    async def rm_bl(c, m):
        if not await need(m) or len(m.command) < 2:
            return
        w = m.text.split(None, 1)[1].lower().strip()
        q("DELETE FROM blacklist WHERE chat_id=? AND word=?", (m.chat.id, w))
        words(m.chat.id).discard(w)
        await m.reply("✅ Removed.")

    @bot.on_message(filters.command("blacklist") & filters.group)
    async def show_bl(c, m):
        ws = sorted(words(m.chat.id))
        await m.reply("🚫 Blacklist: " + (", ".join(f"<code>{html.escape(w)}</code>" for w in ws) if ws else "empty"),
                      parse_mode=HTML)

    @bot.on_message(filters.group & ~filters.service, group=-2)
    async def guard(c, m):
        if not m.from_user:
            return
        chat_id, uid = m.chat.id, m.from_user.id
        cf = cfg(chat_id)
        bad = None

        # anti-flood
        n = int(cf.get("flood", 0) or 0)
        if n:
            dq = flood[(chat_id, uid)]
            now = time.monotonic()
            dq.append(now)
            while dq and now - dq[0] > 6:
                dq.popleft()
            if len(dq) > n:
                dq.clear()
                if not await is_admin(chat_id, uid):
                    try:
                        await c.restrict_chat_member(chat_id, uid, ChatPermissions(),
                                                     until_date=datetime.now() + timedelta(minutes=10))
                        asyncio.create_task(temp(m, f"🌊 {await name_of(uid)} muted for 10 min (flooding).", 8))
                    except Exception:
                        pass
                    return m.stop_propagation()

        text = (m.text or m.caption or "")
        # anti-link
        if cf.get("antilink") == "1":
            ents = (m.entities or []) + (m.caption_entities or [])
            has_entity = any(e.type in (enums.MessageEntityType.URL, enums.MessageEntityType.TEXT_LINK) for e in ents)
            if has_entity or LINK_RE.search(text):
                bad = "link"
        # blacklist
        if not bad and text:
            low = text.lower()
            if any(w in low for w in words(chat_id)):
                bad = "word"
        if bad and not await is_admin(chat_id, uid):
            try:
                await m.delete()
                asyncio.create_task(temp(m, f"🚫 {await name_of(uid)}, "
                                            + ("links aren't allowed here." if bad == "link" else "that word is blacklisted."), 5))
            except Exception:
                pass
            return m.stop_propagation()

    # ───────── info ─────────
    @bot.on_message(filters.command("id"))
    async def id_cmd(c, m):
        lines = []
        if m.reply_to_message and m.reply_to_message.from_user:
            lines.append(f"👤 User ID: <code>{m.reply_to_message.from_user.id}</code>")
        elif m.from_user:
            lines.append(f"👤 Your ID: <code>{m.from_user.id}</code>")
        if m.chat.type != enums.ChatType.PRIVATE:
            lines.append(f"💬 Chat ID: <code>{m.chat.id}</code>")
        await m.reply("\n".join(lines), parse_mode=HTML)

    @bot.on_message(filters.command("info") & filters.group)
    async def info(c, m):
        uid, _ = await get_target(m)
        uid = uid or (m.from_user.id if m.from_user else None)
        try:
            u = await c.get_users(uid)
            mem = await c.get_chat_member(m.chat.id, uid)
            row = q("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (m.chat.id, uid))
            await m.reply(
                f"👤 <b>{html.escape(u.first_name or '')}</b>\n"
                f"🆔 <code>{u.id}</code>\n"
                f"🔖 {'@' + u.username if u.username else 'no username'}\n"
                f"🛡 Status: <b>{mem.status.name.title()}</b>\n"
                f"⚠️ Warns: <b>{row[0][0] if row else 0}</b>",
                parse_mode=HTML,
            )
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("admins") & filters.group)
    async def admins(c, m):
        lines = []
        try:
            async for mem in c.get_chat_members(m.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
                if mem.user.is_bot:
                    continue
                crown = "👑" if mem.status == enums.ChatMemberStatus.OWNER else "🛡"
                title = f" — <i>{html.escape(mem.custom_title)}</i>" if getattr(mem, "custom_title", None) else ""
                lines.append(f"{crown} {html.escape(mem.user.first_name or '')}{title}")
            await m.reply("<b>Group admins</b>\n\n" + "\n".join(lines), parse_mode=HTML)
        except Exception as e:
            await fail(m, e)

    @bot.on_message(filters.command("report") & filters.group)
    async def report(c, m):
        if not m.reply_to_message:
            return await m.reply("Reply to the message you want to report.")
        pings = []
        try:
            async for mem in c.get_chat_members(m.chat.id, filter=enums.ChatMembersFilter.ADMINISTRATORS):
                if not mem.user.is_bot and len(pings) < 5:
                    pings.append(f'<a href="tg://user?id={mem.user.id}">\u200b</a>')
        except Exception:
            pass
        await m.reply("🚨 Reported to the admins." + "".join(pings), parse_mode=HTML)

    @bot.on_message(filters.command("ping"))
    async def ping(c, m):
        t = time.monotonic()
        msg = await m.reply("🏓 Pong…")
        await msg.edit(f"🏓 Pong! <b>{(time.monotonic() - t) * 1000:.0f} ms</b>", parse_mode=HTML)

    @bot.on_message(filters.command("stats") & filters.user(OWNER_ID))
    async def stats(c, m):
        groups = q("SELECT COUNT(*) FROM chats WHERE kind='group'")[0][0]
        users = q("SELECT COUNT(*) FROM chats WHERE kind='user'")[0][0]
        fb = q("SELECT COUNT(*) FROM fbans")[0][0]
        await m.reply(f"📊 <b>Matrix Supreme stats</b>\n\nGroups: <b>{groups}</b>\nUsers: <b>{users}</b>\nFbanned: <b>{fb}</b>",
                      parse_mode=HTML)
