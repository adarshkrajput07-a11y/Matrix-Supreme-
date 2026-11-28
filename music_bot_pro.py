import asyncio
import html
import os
import random
import time

import aiohttp
import yt_dlp
from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait, UserAlreadyParticipant
from pyrogram.types import (InlineKeyboardButton as Btn,
                            InlineKeyboardMarkup as Markup)
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream, Update, StreamEnded

from card_image import make_card_image

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.environ["SESSION_STRING"]
BOT_NAME = os.getenv("BOT_NAME", "Matrix Supreme")

# Optional: YouTube cookies (Netscape format) in a COOKIES variable
COOKIES_FILE = None
if os.getenv("COOKIES"):
    COOKIES_FILE = "/tmp/cookies.txt"
    with open(COOKIES_FILE, "w") as f:
        f.write(os.environ["COOKIES"])

bot = Client("bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
             in_memory=True)
assistant = Client("assistant", api_id=API_ID, api_hash=API_HASH,
                   session_string=SESSION_STRING)
call = PyTgCalls(assistant)

queues = {}          # chat_id -> [track dicts]; queues[chat][0] is playing
paused = set()
autoplay_off = set()  # chats with autoplay disabled (default: ON)
history = {}          # chat_id -> recent video ids
progress_tasks = {}   # chat_id -> asyncio task

QUOTES = [
    "Where words fail, music speaks.",
    "Life is a song, sing it loud.",
    "Music is the shorthand of emotion.",
    "Turn up the volume, turn down the world.",
    "One good thing about music: when it hits you, you feel no pain.",
]


# ───────────────────────── helpers ─────────────────────────
def is_auto(chat_id):
    return chat_id not in autoplay_off


def fmt_time(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def dur_text(sec):
    sec = int(sec or 0)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def is_current(chat_id, t):
    q = queues.get(chat_id)
    return bool(q) and q[0] is t


def elapsed(t, is_paused):
    if not t.get("started"):
        return 0
    end = t["paused_at"] if (is_paused and t.get("paused_at")) else time.time()
    el = end - t["started"] - t.get("paused_total", 0)
    return max(0, min(el, t["duration"] or el))


def progress_text(t, is_paused, n=10):
    el, dur = elapsed(t, is_paused), t["duration"] or 0
    pos = min(int(n * el / dur), n - 1) if dur else 0
    return f"{fmt_time(el)} " + "━" * pos + "🟣" + "━" * (n - 1 - pos) + f" {fmt_time(dur)}"


def mark_pause(chat_id, pause: bool):
    q = queues.get(chat_id)
    if not q:
        return
    t = q[0]
    if pause and chat_id not in paused:
        t["paused_at"] = time.time()
        paused.add(chat_id)
    elif not pause and chat_id in paused:
        t["paused_total"] = t.get("paused_total", 0) + \
            time.time() - (t.get("paused_at") or time.time())
        paused.discard(chat_id)


# ───────────────────────── YouTube / audio ─────────────────────────
def get_track(query: str, requester: str):
    opts = {"format": "bestaudio/best", "quiet": True, "noplaylist": True}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if query.startswith("http"):
        targets = [query]
    else:
        targets = [f"ytsearch:{query}", f"scsearch:{query}"]
    info, last_err = None, None
    for target in targets:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
            if "entries" in info:
                info = info["entries"][0]
            break
        except Exception as e:
            info, last_err = None, e
    if info is None:
        raise last_err

    is_yt = str(info.get("extractor_key", "")).lower().startswith("youtube")
    if is_yt:
        thumbs = [f"https://i.ytimg.com/vi/{info['id']}/maxresdefault.jpg",
                  f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"]
    else:
        thumbs = [info.get("thumbnail")]
    return {
        "id": info["id"],
        "title": info["title"],
        "url": info["url"],
        "page": info.get("webpage_url"),
        "thumbs": [u for u in thumbs if u],
        "duration": info.get("duration"),
        "channel": info.get("uploader") or info.get("channel") or "Unknown",
        "by": requester,
    }


def get_related(last, played):
    """Pick a related song using YouTube's Mix, falling back to a channel search."""
    opts = {"quiet": True, "extract_flat": True, "playlistend": 20}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    vid = last["id"]
    sources = [f"https://www.youtube.com/watch?v={vid}&list=RD{vid}",
               f"ytsearch10:{last['channel']} songs"]
    for src in sources:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(src, download=False)
            for e in info.get("entries") or []:
                if e.get("id") and e["id"] not in played:
                    return e["id"]
        except Exception:
            continue
    return None


def search_results(query: str, n: int = 5):
    """Top search results (title, singer/channel, duration) without resolving audio."""
    opts = {"quiet": True, "extract_flat": True, "skip_download": True}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    for prefix in ("ytsearch", "scsearch"):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
        except Exception:
            continue
        out = []
        for e in info.get("entries") or []:
            if not e.get("id"):
                continue
            url = e.get("url") or e.get("webpage_url") or \
                f"https://www.youtube.com/watch?v={e['id']}"
            out.append({
                "url": url,
                "title": e.get("title") or "Unknown",
                "channel": e.get("uploader") or e.get("channel") or "Unknown",
                "duration": e.get("duration"),
            })
        if out:
            return out
    return []


async def fetch_bytes(url):
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
                if r.status == 200:
                    return await r.read()
    except Exception:
        pass
    return None


async def get_art(t):
    for url in t["thumbs"]:
        data = await fetch_bytes(url)
        if data:
            return data
    return None


# ───────────────────────── card / buttons ─────────────────────────
def card(t, status):
    title = html.escape(t["title"])
    if t.get("page"):
        title = f'<a href="{t["page"]}">{title}</a>'
    return (
        f"<b>{status}</b>\n\n"
        f"<blockquote>💜 <b>Title:</b> {title}\n"
        f"🔮 <b>Duration:</b> {dur_text(t['duration'])}\n"
        f"🟣 <b>Requested By:</b> {t['by']}</blockquote>\n\n"
        f"<blockquote expandable>❝ <i>{random.choice(QUOTES)}</i> ❞</blockquote>\n\n"
        f"<b>💜 {BOT_NAME}</b>"
    )


def B(text, data, style=None):
    """Inline button. `style` (primary/success/danger) needs a library that
    supports Bot API 9.4+; older ones fall back to a plain button."""
    if style:
        try:
            return Btn(text, callback_data=data, style=style)
        except TypeError:
            pass
    return Btn(text, callback_data=data)


def controls(chat_id, t, is_paused=None):
    if is_paused is None:
        is_paused = chat_id in paused
    rows = []
    if is_current(chat_id, t):
        rows.append([B(progress_text(t, is_paused), "noop", "primary")])
    pr = B("🟣 Resume", "resume", "primary") if is_paused \
        else B("🟣 Pause", "pause", "primary")
    rows.append([B("🟣 Replay", "replay", "primary"), pr,
                 B("🟣 Skip", "skip", "primary")])
    n = max(len(queues.get(chat_id, [])) - 1, 0)
    rows.append([B(f"🟣 Queue · {n}", "queue")])
    rows.append([B("🟣 Stop", "stop"),
                 B(f"🟣 Autoplay: {'ON' if is_auto(chat_id) else 'OFF'}", "autoplay")])
    return Markup(rows)


def stop_progress(chat_id):
    task = progress_tasks.pop(chat_id, None)
    if task:
        task.cancel()


async def progress_loop(chat_id, t):
    """Refresh the progress button of the current card every 10 seconds."""
    try:
        while is_current(chat_id, t) and t.get("msg_id"):
            await asyncio.sleep(10)
            if chat_id in paused or not is_current(chat_id, t):
                continue
            try:
                await bot.edit_message_reply_markup(
                    chat_id, t["msg_id"], controls(chat_id, t))
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass
    except asyncio.CancelledError:
        pass


async def send_card(chat_id, t, status="💜 Started Streaming On VC."):
    kb, cap = controls(chat_id, t), card(t, status)
    try:
        art = await get_art(t)
        img = await asyncio.to_thread(make_card_image, art, t["title"],
                                      t["channel"], fmt_time(t["duration"]))
        msg = await bot.send_photo(chat_id, img, caption=cap, reply_markup=kb,
                                   parse_mode=ParseMode.HTML)
    except Exception:
        msg = await bot.send_message(chat_id, cap, reply_markup=kb,
                                     parse_mode=ParseMode.HTML)
    t["msg_id"] = msg.id
    if is_current(chat_id, t):
        stop_progress(chat_id)
        progress_tasks[chat_id] = asyncio.create_task(progress_loop(chat_id, t))
    return msg


# ───────────────────────── streaming ─────────────────────────
async def start_stream(chat_id):
    q = queues.get(chat_id, [])
    if not q:
        paused.discard(chat_id)
        stop_progress(chat_id)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        return None
    t = q[0]
    await call.play(chat_id, MediaStream(t["url"],
                                         video_flags=MediaStream.Flags.IGNORE))
    paused.discard(chat_id)
    t["started"], t["paused_total"], t["paused_at"] = time.time(), 0, None
    h = history.setdefault(chat_id, [])
    if not h or h[-1] != t["id"]:
        h.append(t["id"])
    del h[:-50]
    return t


async def strip_buttons(chat_id, t):
    if t and t.get("msg_id"):
        try:
            await bot.edit_message_reply_markup(chat_id, t["msg_id"], None)
        except Exception:
            pass


async def do_skip(chat_id):
    q = queues.get(chat_id, [])
    last = q.pop(0) if q else None
    await strip_buttons(chat_id, last)
    status = "💜 Started Streaming On VC."
    if not q and last and is_auto(chat_id):
        vid = await asyncio.to_thread(get_related, last,
                                      set(history.get(chat_id, [])))
        if vid:
            try:
                t = await asyncio.to_thread(
                    get_track, f"https://www.youtube.com/watch?v={vid}",
                    "🤖 Autoplay")
                q.append(t)
                status = "🔁 Autoplay"
            except Exception:
                pass
    t = await start_stream(chat_id)
    if t:
        await send_card(chat_id, t, status)
    else:
        await bot.send_message(chat_id, "✅ Queue finished.")


async def ensure_assistant(chat_id, username=None):
    """Get the assistant into the group. Returns (ok, error_text)."""
    errs = []

    async def known():
        try:
            await assistant.get_chat(chat_id)
            return True
        except Exception as e:
            errs.append(f"get_chat: {e}")
            return False

    if await known():
        return True, ""
    if username:
        try:
            await assistant.join_chat(username)
        except UserAlreadyParticipant:
            pass
        except Exception as e:
            errs.append(f"join @{username}: {e}")
        if await known():
            return True, ""
    try:
        link = await bot.export_chat_invite_link(chat_id)
    except Exception as e:
        errs.append(f"invite link: {e}")
        return False, "\n".join(errs[-3:])
    try:
        await assistant.join_chat(link)
    except UserAlreadyParticipant:
        try:
            await assistant.get_chat(link)
        except Exception as e:
            errs.append(f"resolve: {e}")
    except Exception as e:
        errs.append(f"join link: {e}")
    if await known():
        return True, ""
    return False, "\n".join(errs[-3:])


# ───────────────────────── commands ─────────────────────────
@bot.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(
        f"<b>💜 {BOT_NAME}</b>\n\n"
        "<blockquote>Add me to a group, start a voice chat and use:\n"
        "/play &lt;song&gt; • /fplay • /autoplay • /skip • /pause • /resume • "
        "/stop • /queue • /tagall</blockquote>",
        parse_mode=ParseMode.HTML)


pending = {}   # (chat_id, message_id) -> search list waiting for a choice


async def add_song(chat_id, target, by, msg):
    """Load a song (URL or search text), queue it and start playing if idle."""
    try:
        t = await asyncio.to_thread(get_track, target, by)
    except Exception as e:
        return await msg.edit(
            "❌ <b>Couldn't load that song.</b>\n"
            f"<blockquote expandable><code>{html.escape(str(e)[:350])}</code></blockquote>",
            parse_mode=ParseMode.HTML)
    q = queues.setdefault(chat_id, [])
    q.append(t)
    try:
        await msg.delete()
    except Exception:
        pass
    if len(q) == 1:
        try:
            await start_stream(chat_id)
        except Exception as e:
            q.clear()
            return await bot.send_message(
                chat_id, f"❌ Can't join voice chat: <code>{html.escape(str(e))}</code>",
                parse_mode=ParseMode.HTML)
        await send_card(chat_id, t)
    else:
        await send_card(chat_id, t, f"🟣 Added To Queue (#{len(q) - 1})")


async def expire_pending(key, msg, seconds=120):
    await asyncio.sleep(seconds)
    if pending.pop(key, None):
        try:
            await msg.delete()
        except Exception:
            pass


@bot.on_message(filters.command(["play", "p", "fplay", "fp"]) & filters.group)
async def play(_, m):
    if len(m.command) < 2:
        return await m.reply("Usage: /play <song name>")
    query = " ".join(m.command[1:])
    by = m.from_user.mention if m.from_user else "Anonymous"
    force = m.command[0].lower() in ("fplay", "fp") or query.startswith("http")
    msg = await m.reply("🔎 <b>Searching...</b>", parse_mode=ParseMode.HTML)
    ok, err = await ensure_assistant(m.chat.id, m.chat.username)
    if not ok:
        return await msg.edit(
            "❌ <b>Assistant can't join this group.</b>\n"
            "Make me admin with <b>Invite users</b> permission, or add the "
            "assistant account manually.\n\n"
            f"<blockquote expandable><code>{html.escape(err)}</code></blockquote>",
            parse_mode=ParseMode.HTML)
    if force:   # /fplay or a direct link: play the top result straight away
        return await add_song(m.chat.id, query, by, msg)

    results = await asyncio.to_thread(search_results, query)
    if not results:
        return await msg.edit("❌ No results found. Try a different name.")
    lines = []
    for i, r in enumerate(results, 1):
        dur = fmt_time(r["duration"]) if r["duration"] else "LIVE"
        lines.append(f"<b>{i}.</b> {html.escape(r['title'][:60])}\n"
                     f"     👤 {html.escape(r['channel'][:30])}  •  ⏱ {dur}")
    text = (f"🔎 <b>Results for:</b> <i>{html.escape(query[:50])}</i>\n\n"
            "<blockquote>" + "\n".join(lines) + "</blockquote>\n\n"
            "👇 <b>Tap the number of the correct song</b>")
    kb = Markup([
        [B(f"🟣 {i}", f"pick:{i - 1}", "primary") for i in range(1, len(results) + 1)],
        [B("🟣 Cancel", "pickcancel")],
    ])
    await msg.edit(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    key = (m.chat.id, msg.id)
    pending[key] = {"results": results, "user": m.from_user.id if m.from_user else 0,
                    "by": by}
    asyncio.create_task(expire_pending(key, msg))


async def handle_pick(cb):
    key = (cb.message.chat.id, cb.message.id)
    p = pending.get(key)
    if not p:
        return await cb.answer("This list expired. Use /play again.", show_alert=True)
    if cb.from_user.id != p["user"]:
        return await cb.answer("Only the person who used /play can choose.",
                               show_alert=True)
    pending.pop(key, None)
    if cb.data == "pickcancel":
        await cb.answer("Cancelled")
        return await cb.message.delete()
    r = p["results"][int(cb.data.split(":")[1])]
    await cb.answer("▶️ Loading...")
    await cb.message.edit(f"⏳ <b>Loading:</b> {html.escape(r['title'][:60])}",
                          parse_mode=ParseMode.HTML)
    await add_song(cb.message.chat.id, r["url"], p["by"], cb.message)


async def queue_text(chat_id):
    q = queues.get(chat_id, [])
    if not q:
        return "Queue is empty."
    lines = [f"<b>▶️ Now:</b> {html.escape(q[0]['title'])}"]
    lines += [f"<b>{i}.</b> {html.escape(t['title'])}"
              for i, t in enumerate(q[1:11], 1)]
    return "<b>📜 Queue</b>\n\n<blockquote>" + "\n".join(lines) + "</blockquote>"


@bot.on_message(filters.command("queue") & filters.group)
async def queue_cmd(_, m):
    await m.reply(await queue_text(m.chat.id), parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("autoplay") & filters.group)
async def autoplay_cmd(_, m):
    arg = m.command[1].lower() if len(m.command) > 1 else ""
    if arg == "on":
        autoplay_off.discard(m.chat.id)
    elif arg == "off":
        autoplay_off.add(m.chat.id)
    else:
        autoplay_off.symmetric_difference_update({m.chat.id})
    state = "ON ✅" if is_auto(m.chat.id) else "OFF ❌"
    await m.reply(f"🔁 <b>Autoplay:</b> {state}", parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("skip") & filters.group)
async def skip_cmd(_, m):
    await do_skip(m.chat.id)


async def do_stop(chat_id):
    q = queues.get(chat_id, [])
    if q:
        await strip_buttons(chat_id, q[0])
    queues[chat_id] = []
    await start_stream(chat_id)


@bot.on_message(filters.command("stop") & filters.group)
async def stop_cmd(_, m):
    await do_stop(m.chat.id)
    await m.reply("⏹ <b>Stopped.</b>", parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, m):
    await call.pause(m.chat.id)
    mark_pause(m.chat.id, True)
    await m.reply("⏸ <b>Paused.</b>", parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, m):
    await call.resume(m.chat.id)
    mark_pause(m.chat.id, False)
    await m.reply("▶️ <b>Resumed.</b>", parse_mode=ParseMode.HTML)


# ───────────────────────── /tagall ─────────────────────────
tag_running = set()


async def is_admin(m):
    if not m.from_user:   # anonymous admin posting as the group
        return bool(m.sender_chat and m.sender_chat.id == m.chat.id)
    try:
        mem = await bot.get_chat_member(m.chat.id, m.from_user.id)
        return mem.status in (ChatMemberStatus.OWNER,
                              ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False


async def collect_members(client, chat_id):
    users = []
    async for mem in client.get_chat_members(chat_id):
        u = mem.user
        if u and not u.is_bot and not u.is_deleted:
            users.append(u)
    return users


@bot.on_message(filters.command(["tagall", "all"]) & filters.group)
async def tagall(_, m):
    cid = m.chat.id
    if not await is_admin(m):
        return await m.reply("❌ Only group admins can use /tagall.")
    if cid in tag_running:
        return await m.reply("Already tagging. Send /cancel to stop.")
    text = html.escape(" ".join(m.command[1:])) or "📢 <b>Attention everyone!</b>"
    note = await m.reply("👥 <b>Collecting members...</b>",
                         parse_mode=ParseMode.HTML)

    # The bot can only mention people it has "seen". It lists members first;
    # if it sees too few (large groups), the assistant account lists and tags.
    sender, users, total = bot, [], 0
    try:
        users = await collect_members(bot, cid)
        total = await bot.get_chat_members_count(cid)
    except Exception:
        pass
    if not users or len(users) < total * 0.7:
        ok, err = await ensure_assistant(cid, m.chat.username)
        if ok:
            try:
                alt = await collect_members(assistant, cid)
                if len(alt) > len(users):
                    users, sender = alt, assistant
            except Exception:
                pass
    if not users:
        return await note.edit("❌ Couldn't get the member list.")

    await note.edit(f"📢 <b>Tagging {len(users)} members...</b>\n"
                    "Send /cancel to stop.", parse_mode=ParseMode.HTML)
    tag_running.add(cid)
    sent, completed = 0, False
    try:
        for i in range(0, len(users), 5):
            if cid not in tag_running:
                break
            chunk = users[i:i + 5]
            names = " ".join(
                f'<a href="tg://user?id={u.id}">'
                f'{html.escape((u.first_name or "User")[:12])}</a>'
                for u in chunk)
            for _attempt in range(2):
                try:
                    await sender.send_message(
                        cid, f"{text}\n\n{names}", parse_mode=ParseMode.HTML)
                    sent += len(chunk)
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception:
                    break
            await asyncio.sleep(3)
        else:
            completed = True
    finally:
        tag_running.discard(cid)
    await bot.send_message(
        cid, f"✅ <b>Tagged {sent} members.</b>" if completed
        else f"⏹ <b>Tagging stopped after {sent} members.</b>",
        parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("cancel") & filters.group)
async def cancel_tag(_, m):
    if await is_admin(m):
        tag_running.discard(m.chat.id)


# ───────────────────────── buttons ─────────────────────────
@bot.on_callback_query()
async def buttons(_, cb):
    chat_id, data = cb.message.chat.id, cb.data
    if data.startswith("pick:") or data == "pickcancel":
        return await handle_pick(cb)
    q = queues.get(chat_id, [])
    if data == "noop":
        return await cb.answer(
            progress_text(q[0], chat_id in paused) if q else "Nothing playing")
    if data == "pause":
        await call.pause(chat_id)
        mark_pause(chat_id, True)
    elif data == "resume":
        await call.resume(chat_id)
        mark_pause(chat_id, False)
    elif data == "replay":
        if not q:
            return await cb.answer("Nothing playing", show_alert=True)
        await start_stream(chat_id)
        await cb.answer("↺ Replaying")
    elif data == "skip":
        await cb.answer("⏭ Skipped")
        return await do_skip(chat_id)
    elif data == "stop":
        await cb.answer("⏹ Stopped")
        return await do_stop(chat_id)
    elif data == "autoplay":
        autoplay_off.symmetric_difference_update({chat_id})
        await cb.answer("Autoplay " + ("ON" if is_auto(chat_id) else "OFF"))
    elif data == "queue":
        return await cb.answer(
            "\n".join(f"{i}. {t['title'][:40]}" for i, t in enumerate(q[:6]))
            or "Queue is empty", show_alert=True)
    # refresh the buttons of the card that was tapped
    if q and cb.message.id == q[0].get("msg_id"):
        try:
            await cb.message.edit_reply_markup(controls(chat_id, q[0]))
        except Exception:
            pass
    try:
        await cb.answer()
    except Exception:
        pass


@call.on_update()
async def on_update(_, update: Update):
    if isinstance(update, StreamEnded):
        await do_skip(update.chat_id)


async def main():
    await bot.start()
    await assistant.start()
    await call.start()
    await idle()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
