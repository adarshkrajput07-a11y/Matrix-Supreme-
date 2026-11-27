import asyncio
import os
import html
import random
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import UserAlreadyParticipant
from pyrogram.types import (InlineKeyboardButton as Btn,
                            InlineKeyboardMarkup as Markup)
from pytgcalls import PyTgCalls, idle
from pytgcalls.types import MediaStream, Update, StreamEnded
import yt_dlp

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

queues = {}   # chat_id -> [track dicts]
paused = set()
autoplay_off = set()   # chats where autoplay is disabled (default: ON)
history = {}           # chat_id -> recently played video ids


def is_auto(chat_id):
    return chat_id not in autoplay_off

QUOTES = [
    "Where words fail, music speaks.",
    "Life is a song, sing it loud.",
    "Music is the shorthand of emotion.",
    "Turn up the volume, turn down the world.",
    "One good thing about music: when it hits you, you feel no pain.",
]


def fmt_time(sec):
    sec = int(sec or 0)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_views(n):
    n = int(n or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return str(n)


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
    return {
        "id": info["id"],
        "title": info["title"],
        "url": info["url"],
        "page": info.get("webpage_url"),
        "thumb": (f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"
                  if str(info.get("extractor_key", "")).lower().startswith("youtube")
                  else info.get("thumbnail")),
        "duration": info.get("duration"),
        "channel": info.get("uploader", "Unknown"),
        "views": info.get("view_count"),
        "by": requester,
    }


def get_related(last, played):
    """Pick a related song using YouTube's Mix, falling back to a channel search."""
    opts = {"quiet": True, "extract_flat": True, "playlistend": 20}
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


def card(t, status="🎧 𝗡𝗼𝘄 𝗣𝗹𝗮𝘆𝗶𝗻𝗴"):
    return (
        f"<b>{status}</b>\n\n"
        f"<blockquote><b>🎵 {html.escape(t['title'])}</b>\n"
        f"👤 {html.escape(t['channel'])}\n"
        f"⏱ {fmt_time(t['duration'])}  •  👁 {fmt_views(t['views'])}</blockquote>\n"
        f"🙋 <b>Requested by:</b> {t['by']}\n\n"
        f"<blockquote expandable>❝ <i>{random.choice(QUOTES)}</i> ❞</blockquote>\n\n"
        f"<b>🎶 {BOT_NAME}</b>"
    )


def controls(chat_id, t, is_paused=False):
    pr = Btn("▶️ Resume", callback_data="resume") if is_paused \
        else Btn("⏸ Pause", callback_data="pause")
    rows = [
        [pr, Btn("⏭ Skip", callback_data="skip"),
         Btn("⏹ Stop", callback_data="stop")],
        [Btn("📜 Queue", callback_data="queue"),
         Btn(f"🔁 Autoplay: {'ON' if is_auto(chat_id) else 'OFF'}",
             callback_data="autoplay")],
    ]
    if t.get("page"):
        rows[1].append(Btn("🔗 Source", url=t["page"]))
    return Markup(rows)


async def send_card(chat_id, t, status="🎧 𝗡𝗼𝘄 𝗣𝗹𝗮𝘆𝗶𝗻𝗴", reply_to=None):
    kb = controls(chat_id, t)
    try:
        return await bot.send_photo(chat_id, t["thumb"], caption=card(t, status),
                                    reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        return await bot.send_message(chat_id, card(t, status), reply_markup=kb,
                                      parse_mode=ParseMode.HTML)


async def start_stream(chat_id):
    q = queues.get(chat_id, [])
    if not q:
        paused.discard(chat_id)
        try:
            await call.leave_call(chat_id)
        except Exception:
            pass
        return None
    t = q[0]
    await call.play(chat_id, MediaStream(t["url"],
                                         video_flags=MediaStream.Flags.IGNORE))
    paused.discard(chat_id)
    h = history.setdefault(chat_id, [])
    h.append(t["id"])
    del h[:-50]
    return t


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

    # public group: join by username
    if username:
        try:
            await assistant.join_chat(username)
        except UserAlreadyParticipant:
            pass
        except Exception as e:
            errs.append(f"join @{username}: {e}")
        if await known():
            return True, ""

    # private group: bot makes an invite link, assistant joins with it
    try:
        link = await bot.export_chat_invite_link(chat_id)
    except Exception as e:
        errs.append(f"invite link: {e}")
        return False, "\n".join(errs[-3:])
    try:
        await assistant.join_chat(link)
    except UserAlreadyParticipant:
        try:
            await assistant.get_chat(link)   # caches the group for the assistant
        except Exception as e:
            errs.append(f"resolve: {e}")
    except Exception as e:
        errs.append(f"join link: {e}")
    if await known():
        return True, ""
    return False, "\n".join(errs[-3:])


@bot.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(
        f"<b>🎶 {BOT_NAME}</b>\n\n"
        "<blockquote>Add me to a group, start a voice chat and use:\n"
        "/play &lt;song&gt; • /autoplay • /skip • /pause • /resume • /stop • /queue</blockquote>",
        parse_mode=ParseMode.HTML)


@bot.on_message(filters.command(["play", "p"]) & filters.group)
async def play(_, m):
    if len(m.command) < 2:
        return await m.reply("Usage: /play <song name>")
    msg = await m.reply("🔎 <b>Searching...</b>", parse_mode=ParseMode.HTML)
    ok, err = await ensure_assistant(m.chat.id, m.chat.username)
    if not ok:
        return await msg.edit(
            "❌ <b>Assistant can't join this group.</b>\n"
            "Make me admin with <b>Invite users</b> permission, or add the "
            "assistant account manually.\n\n"
            f"<blockquote expandable><code>{html.escape(err)}</code></blockquote>",
            parse_mode=ParseMode.HTML)
    try:
        t = await asyncio.to_thread(get_track, " ".join(m.command[1:]),
                                    m.from_user.mention)
    except Exception as e:
        return await msg.edit(
            "❌ <b>Couldn't load that song.</b>\n"
            f"<blockquote expandable><code>{html.escape(str(e)[:350])}</code></blockquote>",
            parse_mode=ParseMode.HTML)
    q = queues.setdefault(m.chat.id, [])
    q.append(t)
    await msg.delete()
    if len(q) == 1:
        try:
            await start_stream(m.chat.id)
        except Exception as e:
            q.clear()
            return await m.reply(f"❌ Can't join voice chat: <code>{e}</code>",
                                 parse_mode=ParseMode.HTML)
        await send_card(m.chat.id, t)
    else:
        await send_card(m.chat.id, t, f"➕ 𝗤𝘂𝗲𝘂𝗲𝗱 #{len(q) - 1}")


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


@bot.on_message(filters.command("stop") & filters.group)
async def stop_cmd(_, m):
    queues[m.chat.id] = []
    await start_stream(m.chat.id)
    await m.reply("⏹ <b>Stopped.</b>", parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_cmd(_, m):
    await call.pause(m.chat.id)
    paused.add(m.chat.id)
    await m.reply("⏸ <b>Paused.</b>", parse_mode=ParseMode.HTML)


@bot.on_message(filters.command("resume") & filters.group)
async def resume_cmd(_, m):
    await call.resume(m.chat.id)
    paused.discard(m.chat.id)
    await m.reply("▶️ <b>Resumed.</b>", parse_mode=ParseMode.HTML)


async def do_skip(chat_id):
    q = queues.get(chat_id, [])
    last = q.pop(0) if q else None
    status = "🎧 𝗡𝗼𝘄 𝗣𝗹𝗮𝘆𝗶𝗻𝗴"
    if not q and last and is_auto(chat_id):
        vid = await asyncio.to_thread(get_related, last,
                                      set(history.get(chat_id, [])))
        if vid:
            try:
                t = await asyncio.to_thread(
                    get_track, f"https://www.youtube.com/watch?v={vid}",
                    "🤖 Autoplay")
                q.append(t)
                status = "🔁 𝗔𝘂𝘁𝗼𝗽𝗹𝗮𝘆"
            except Exception:
                pass
    t = await start_stream(chat_id)
    if t:
        await send_card(chat_id, t, status)
    else:
        await bot.send_message(chat_id, "✅ Queue finished.")


@bot.on_callback_query()
async def buttons(_, cb):
    chat_id, data = cb.message.chat.id, cb.data
    q = queues.get(chat_id, [])
    if data == "pause":
        await call.pause(chat_id)
        paused.add(chat_id)
    elif data == "resume":
        await call.resume(chat_id)
        paused.discard(chat_id)
    elif data == "skip":
        await cb.answer("⏭ Skipped")
        return await do_skip(chat_id)
    elif data == "stop":
        queues[chat_id] = []
        await start_stream(chat_id)
        await cb.answer("⏹ Stopped")
        return await cb.message.edit_reply_markup(None)
    elif data == "autoplay":
        autoplay_off.symmetric_difference_update({chat_id})
        await cb.answer("Autoplay " + ("ON" if is_auto(chat_id) else "OFF"))
        if q:
            await cb.message.edit_reply_markup(
                controls(chat_id, q[0], chat_id in paused))
        return
    elif data == "queue":
        return await cb.answer(
            "\n".join(f"{i}. {t['title'][:40]}" for i, t in enumerate(q[:6]))
            or "Queue is empty", show_alert=True)
    if q:
        await cb.message.edit_reply_markup(
            controls(chat_id, q[0], chat_id in paused))
    await cb.answer()


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
