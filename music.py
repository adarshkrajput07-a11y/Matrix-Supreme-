"""Premium music module: now-playing card, live progress bar, inline controls."""
import asyncio
import base64
import html
import os
import random
import time
from dataclasses import dataclass, field

import yt_dlp
from pyrogram import enums, filters
from pyrogram.errors import FloodWait, MessageNotModified, InviteRequestSent, UserAlreadyParticipant, UserBannedInChannel, UserNotParticipant
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as M
from pytgcalls import filters as fl
from pytgcalls.types import AudioQuality, MediaStream

HTML = enums.ParseMode.HTML
def _cookie_file():
    """Optional YouTube cookies (Railway variable YT_COOKIES or YT_COOKIES_B64)."""
    data = None
    if os.getenv("YT_COOKIES_B64"):
        data = base64.b64decode(os.environ["YT_COOKIES_B64"]).decode()
    elif os.getenv("YT_COOKIES"):
        data = os.environ["YT_COOKIES"]
    if not data:
        return None
    path = "/tmp/yt_cookies.txt"
    with open(path, "w") as f:
        f.write(data)
    return path


YDL = {"format": "bestaudio/best", "quiet": True, "noplaylist": True, "default_search": "ytsearch"}
if _cookie_file():
    YDL["cookiefile"] = _cookie_file()

_bot = _assistant = _call = _is_admin = _q = None


# ───────────────────────── Data ─────────────────────────
@dataclass
class Track:
    title: str
    page_url: str
    stream_url: str
    thumb: str | None
    duration: int
    req_id: int = 0
    req_name: str = "Autoplay"


@dataclass
class State:
    queue: list = field(default_factory=list)
    current: Track | None = None
    loop: str = "off"  # off | one | all
    autoplay: bool = False
    volume: int = 100
    paused: bool = False
    pos: float = 0.0  # position at the last anchor
    anchor: float | None = None  # monotonic time playback (re)started; None when paused
    card_id: int | None = None
    card_photo: bool = False
    updater: asyncio.Task | None = None
    switching: bool = False
    thumb_on: bool = True
    history: set = field(default_factory=set)

    def position(self) -> float:
        p = self.pos + ((time.monotonic() - self.anchor) if self.anchor else 0)
        d = self.current.duration if self.current else 0
        return min(p, d) if d else p


states: dict[int, State] = {}


def S(chat_id: int) -> State:
    return states.setdefault(chat_id, State())


# ───────────────────────── Helpers ─────────────────────────
def fmt(s: float) -> str:
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def bar(pos: float, dur: int, n: int = 14) -> str:
    if not dur:
        return f"{fmt(pos)}  ━━━━━━━━━━━━━━  LIVE"
    i = min(n - 1, int(pos / dur * n))
    return f"{fmt(pos)}  " + "━" * i + "●" + "─" * (n - 1 - i) + f"  {fmt(dur)}"


def mention(uid: int, name: str) -> str:
    name = html.escape(name)
    return f'<a href="tg://user?id={uid}">{name}</a>' if uid else name


def extract(query: str) -> dict:
    """YouTube first; if it fails on a text search, fall back to SoundCloud."""
    try:
        return _extract(query)
    except Exception as first_error:
        if query.startswith("http"):
            raise
        try:
            return _extract(f"scsearch1:{query}")
        except Exception:
            raise first_error


def _extract(query: str) -> dict:
    with yt_dlp.YoutubeDL(YDL) as y:
        info = y.extract_info(query, download=False)
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise ValueError("No results found")
            info = entries[0]
    vid = info.get("id")
    if str(info.get("extractor_key", "")).lower().startswith("youtube") and vid:
        thumb = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    else:
        thumb = info.get("thumbnail")
    return dict(
        title=info.get("title", "Unknown"),
        page_url=info.get("webpage_url") or query,
        stream_url=info["url"],
        thumb=thumb,
        duration=int(info.get("duration") or 0),
    )


def related(title: str, history: set) -> str | None:
    with yt_dlp.YoutubeDL({**YDL, "extract_flat": True}) as y:
        res = y.extract_info(f"ytsearch8:{title}", download=False)
    for e in res.get("entries", []):
        url = e.get("url") or e.get("webpage_url")
        if url and url not in history:
            return url
    return None


async def ensure_assistant(chat_id: int):
    """Add the assistant to the group if needed and make sure it knows the chat."""
    me = await _assistant.get_me()
    S_ = enums.ChatMemberStatus

    # The bot runs without a saved session, so it has never "met" the assistant account.
    # Resolving by username fixes PEER_ID_INVALID on the bot side.
    if me.username:
        try:
            await _bot.get_users(me.username)
        except Exception:
            pass

    present = None  # None = couldn't tell
    try:
        member = await _bot.get_chat_member(chat_id, me.id)
        if member.status == S_.BANNED:
            try:
                await _bot.unban_chat_member(chat_id, me.id)
            except Exception:
                pass
            present = False
        elif member.status == S_.LEFT or (member.status == S_.RESTRICTED and not member.is_member):
            present = False
        else:
            present = True
    except UserNotParticipant:
        present = False
    except Exception:
        present = None

    if present is not True:
        try:
            link = await _bot.export_chat_invite_link(chat_id)
        except Exception:
            raise RuntimeError(
                "The assistant isn't in this group and I can't add it. "
                "Make me an admin with the 'Invite Users via Link' right."
            )
        try:
            await _assistant.join_chat(link)
        except UserAlreadyParticipant:
            pass
        except UserBannedInChannel:
            raise RuntimeError("The assistant account is banned in this group. Unban it and try again.")
        except InviteRequestSent:  # group uses join approval
            try:
                await _bot.approve_chat_join_request(chat_id, me.id)
            except Exception:
                raise RuntimeError(
                    "The assistant requested to join. Approve its join request, then use /play again."
                )
        await asyncio.sleep(1)

    async def known() -> bool:
        try:
            await _assistant.get_chat(chat_id)
            return True
        except Exception:
            return False

    if await known():
        return
    # refresh the assistant's chat list so it caches this group
    try:
        async for _ in _assistant.get_dialogs():
            pass
    except Exception:
        pass
    if await known():
        return
    # public groups: resolve by username
    try:
        chat = await _bot.get_chat(chat_id)
        if chat.username:
            await _assistant.get_chat(chat.username)
    except Exception:
        pass
    if await known():
        return
    raise RuntimeError(
        "The assistant account can't see this group yet. Send any message from the assistant "
        "account inside the group once, then use /play again."
    )


# ───────────────────────── Card ─────────────────────────
def caption(st: State) -> str:
    t = st.current
    head = "⏸ <b>PAUSED</b>" if st.paused else "▶️ <b>NOW PLAYING</b>"
    loop = {"off": "Off", "one": "Track", "all": "Queue"}[st.loop]
    text = (
        f"{head}\n\n"
        f"🎵 <b>{html.escape(t.title)}</b>\n\n"
        f"<code>{bar(st.position(), t.duration)}</code>\n\n"
        f"👤 {mention(t.req_id, t.req_name)}\n"
        f"✨ Autoplay <b>{'ON' if st.autoplay else 'OFF'}</b>  •  "
        f"🔁 Loop <b>{loop}</b>  •  🔊 <b>{st.volume}%</b>"
    )
    footer = "\n\n💎 <i>Matrix Supreme</i>"
    if st.queue:
        text += f"\n\n⏭ <b>Next:</b> {html.escape(st.queue[0].title[:60])}  (+{len(st.queue) - 1} more)" \
            if len(st.queue) > 1 else f"\n\n⏭ <b>Next:</b> {html.escape(st.queue[0].title[:60])}"
    return text + footer


def keyboard(st: State) -> M:
    loop = {"off": "Off", "one": "Track", "all": "Queue"}[st.loop]
    rows = [
        [B("⏪ 10s", callback_data="mp:back"),
         B("▶️ Resume" if st.paused else "⏸ Pause", callback_data="mp:pause"),
         B("10s ⏩", callback_data="mp:fwd")],
        [B("⏮ Replay", callback_data="mp:replay"),
         B("⏭ Skip", callback_data="mp:skip"),
         B("⭐ Favorite", callback_data="mp:fav")],
        [B(f"🔁 Loop: {loop}", callback_data="mp:loop"),
         B(f"✨ Autoplay: {'ON' if st.autoplay else 'OFF'}", callback_data="mp:auto")],
        [B("🔀 Shuffle", callback_data="mp:shuffle"),
         B(f"📜 Queue ({len(st.queue)})", callback_data="mp:queue"),
         B("⏹ End", callback_data="mp:end")],
        [B("🔉 Vol −", callback_data="mp:vdn"),
         B(f"🔊 {st.volume}%", callback_data="mp:vol"),
         B("🔊 Vol +", callback_data="mp:vup")],
    ]
    rows.append([B(f"🖼 Thumbnail: {'ON' if st.thumb_on else 'OFF'}", callback_data="mp:thumb"),
                 B("✖ Close", callback_data="mp:close")])
    if st.current.page_url.startswith("http"):
        rows.append([B("🎬 Open Source", url=st.current.page_url)])
    return M(rows)


async def refresh_card(chat_id: int):
    st = S(chat_id)
    if not st.current or not st.card_id:
        return
    try:
        if st.card_photo:
            await _bot.edit_message_caption(
                chat_id, st.card_id, caption(st), parse_mode=HTML, reply_markup=keyboard(st)
            )
        else:
            await _bot.edit_message_text(
                chat_id, st.card_id, caption(st), parse_mode=HTML, reply_markup=keyboard(st)
            )
    except (MessageNotModified, FloodWait):
        pass
    except Exception:
        pass


async def _updater(chat_id: int):
    try:
        while True:
            await asyncio.sleep(10)
            st = states.get(chat_id)
            if not st or not st.current:
                return
            if not st.paused:
                await refresh_card(chat_id)
    except asyncio.CancelledError:
        pass


async def send_card(chat_id: int):
    st = S(chat_id)
    if st.updater:
        st.updater.cancel()
    if st.card_id:
        try:
            await _bot.delete_messages(chat_id, st.card_id)
        except Exception:
            pass
    try:
        if not st.thumb_on:
            raise RuntimeError("thumbnail disabled")
        msg = await _bot.send_photo(
            chat_id, st.current.thumb, caption=caption(st), parse_mode=HTML, reply_markup=keyboard(st)
        )
        st.card_photo = True
    except Exception:
        msg = await _bot.send_message(
            chat_id, caption(st), parse_mode=HTML, reply_markup=keyboard(st)
        )
        st.card_photo = False
    st.card_id = msg.id
    st.updater = asyncio.create_task(_updater(chat_id))


# ───────────────────────── Playback core ─────────────────────────
async def start_track(chat_id: int, track: Track, offset: float = 0, new_card: bool = True):
    st = S(chat_id)
    st.switching = True
    asyncio.get_running_loop().call_later(2, lambda: setattr(st, "switching", False))
    params = f"-ss {int(offset)}" if offset else None
    await _call.play(
        chat_id,
        MediaStream(
            track.stream_url,
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,
            ffmpeg_parameters=params,
        ),
    )
    if st.volume != 100:
        try:
            await _call.change_volume_call(chat_id, st.volume)
        except Exception:
            pass
    st.current = track
    st.paused = False
    st.pos = float(offset)
    st.anchor = time.monotonic()
    st.history.add(track.page_url)
    if new_card:
        await send_card(chat_id)
    else:
        await refresh_card(chat_id)


async def enqueue(chat_id: int, info: dict, uid: int, name: str):
    track = Track(**info, req_id=uid, req_name=name)
    st = S(chat_id)
    if st.current:
        st.queue.append(track)
        return len(st.queue), track
    await start_track(chat_id, track)
    return 0, track


async def finish(chat_id: int):
    st = states.pop(chat_id, None)
    if not st:
        return
    if st.updater:
        st.updater.cancel()
    try:
        await _call.leave_call(chat_id)
    except Exception:
        pass
    if st.card_id:
        try:
            text = "⏹ <b>Playback ended.</b>\nUse /play to start again.\n\n💎 <i>Matrix Supreme</i>"
            if st.card_photo:
                await _bot.edit_message_caption(chat_id, st.card_id, text, parse_mode=HTML)
            else:
                await _bot.edit_message_text(chat_id, st.card_id, text, parse_mode=HTML)
        except Exception:
            pass


async def advance(chat_id: int, natural: bool):
    st = S(chat_id)
    cur = st.current
    if natural and cur and st.loop == "one":
        return await start_track(chat_id, cur, new_card=False)
    if cur and st.loop == "all":
        st.queue.append(cur)
    nxt = st.queue.pop(0) if st.queue else None
    if not nxt and st.autoplay and cur:
        try:
            url = await asyncio.to_thread(related, cur.title, st.history)
            if url:
                nxt = Track(**await asyncio.to_thread(extract, url))
        except Exception:
            nxt = None
    if not nxt:
        return await finish(chat_id)
    await start_track(chat_id, nxt)


async def seek(chat_id: int, delta: float):
    st = S(chat_id)
    new = max(0, st.position() + delta)
    if st.current.duration and new >= st.current.duration - 1:
        return await advance(chat_id, natural=False)
    await start_track(chat_id, st.current, offset=new, new_card=False)


# ───────────────────────── Handlers ─────────────────────────
def setup(bot, assistant, call, is_admin, q):
    global _bot, _assistant, _call, _is_admin, _q
    _bot, _assistant, _call, _is_admin, _q = bot, assistant, call, is_admin, q

    q(
        "CREATE TABLE IF NOT EXISTS favs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, "
        "title TEXT, url TEXT, UNIQUE(user_id, url))"
    )

    def who(m):
        if m.from_user:
            return m.from_user.id, m.from_user.first_name
        return 0, "Anonymous"

    async def can_control(chat_id, uid):
        st = S(chat_id)
        return bool(st.current) and (uid == st.current.req_id or await is_admin(chat_id, uid))

    async def guard(m) -> bool:
        st = S(m.chat.id)
        if not st.current:
            await m.reply("Nothing is playing.")
            return False
        uid, _ = who(m)
        if not await can_control(m.chat.id, uid):
            await m.reply("Only admins or the person who requested this song can do that.")
            return False
        return True

    @call.on_update(fl.stream_end)
    async def on_stream_end(_, update):
        st = states.get(update.chat_id)
        if st and not st.switching:
            await advance(update.chat_id, natural=True)

    # ── commands ──
    @bot.on_message(filters.command(["play", "p"]) & filters.group)
    async def play_cmd(c, m):
        query = m.text.split(None, 1)[1] if len(m.command) > 1 else None
        if not query and m.reply_to_message and m.reply_to_message.text:
            query = m.reply_to_message.text
        if not query:
            return await m.reply("Usage: /play <song name or link>")
        msg = await m.reply("🔎 Searching…")
        try:
            info = await asyncio.to_thread(extract, query)
            await ensure_assistant(m.chat.id)
            uid, name = who(m)
            pos, tr = await enqueue(m.chat.id, info, uid, name)
            if pos:
                await msg.edit(
                    f"➕ <b>Queued #{pos}:</b> {html.escape(tr.title)}", parse_mode=HTML
                )
            else:
                await msg.delete()
        except Exception as e:
            err = str(e)
            if type(e).__name__ == "NoActiveGroupCall" or "no active" in err.lower():
                return await msg.edit(
                    "❌ <b>No active voice chat found.</b>\n"
                    "Start a voice chat in this group, then use /play again.",
                    parse_mode=HTML,
                )
            if "PEER_ID_INVALID" in err:
                err = ("Telegram couldn't resolve the assistant account. Give the assistant a "
                       "@username (Settings → Username), send one message from it in this group, "
                       "then try /play again.")
            if "Sign in" in err or "not a bot" in err:
                err = "YouTube is blocking this server (cookies needed)."
            await msg.edit(f"❌ Couldn't play: {err[:250]}\nMake sure a voice chat is started in this group.")

    @bot.on_message(filters.command("pause") & filters.group)
    async def pause_cmd(c, m):
        st = S(m.chat.id)
        if await guard(m) and not st.paused:
            await call.pause(m.chat.id)
            st.pos, st.anchor, st.paused = st.position(), None, True
            await refresh_card(m.chat.id)

    @bot.on_message(filters.command("resume") & filters.group)
    async def resume_cmd(c, m):
        st = S(m.chat.id)
        if await guard(m) and st.paused:
            await call.resume(m.chat.id)
            st.anchor, st.paused = time.monotonic(), False
            await refresh_card(m.chat.id)

    @bot.on_message(filters.command("skip") & filters.group)
    async def skip_cmd(c, m):
        if await guard(m):
            await advance(m.chat.id, natural=False)

    @bot.on_message(filters.command(["end", "stop"]) & filters.group)
    async def end_cmd(c, m):
        if await guard(m):
            await finish(m.chat.id)

    @bot.on_message(filters.command("loop") & filters.group)
    async def loop_cmd(c, m):
        if not await guard(m):
            return
        st = S(m.chat.id)
        if len(m.command) > 1 and m.command[1].lower() in ("off", "one", "all"):
            st.loop = m.command[1].lower()
        else:
            st.loop = {"off": "one", "one": "all", "all": "off"}[st.loop]
        await refresh_card(m.chat.id)

    @bot.on_message(filters.command("shuffle") & filters.group)
    async def shuffle_cmd(c, m):
        if await guard(m):
            random.shuffle(S(m.chat.id).queue)
            await refresh_card(m.chat.id)

    @bot.on_message(filters.command("seek") & filters.group)
    async def seek_cmd(c, m):
        if not await guard(m):
            return
        try:
            await seek(m.chat.id, float(m.command[1]))
        except (IndexError, ValueError):
            await m.reply("Usage: /seek <seconds>  (use a negative number to go back)")

    @bot.on_message(filters.command(["volume", "vol"]) & filters.group)
    async def volume_cmd(c, m):
        if not await guard(m):
            return
        try:
            v = max(1, min(200, int(m.command[1])))
        except (IndexError, ValueError):
            return await m.reply("Usage: /volume <1-200>")
        S(m.chat.id).volume = v
        await call.change_volume_call(m.chat.id, v)
        await refresh_card(m.chat.id)

    @bot.on_message(filters.command(["np", "nowplaying"]) & filters.group)
    async def np_cmd(c, m):
        if S(m.chat.id).current:
            await send_card(m.chat.id)
        else:
            await m.reply("Nothing is playing.")

    @bot.on_message(filters.command(["queue", "q"]) & filters.group)
    async def queue_cmd(c, m):
        st = S(m.chat.id)
        if not st.current:
            return await m.reply("Nothing is playing.")
        lines = [f"🎶 <b>Now:</b> {html.escape(st.current.title)}"]
        for i, t in enumerate(st.queue[:15], 1):
            lines.append(f"<b>{i}.</b> {html.escape(t.title)}")
        if len(st.queue) > 15:
            lines.append(f"…and {len(st.queue) - 15} more")
        await m.reply("\n".join(lines), parse_mode=HTML)

    @bot.on_message(filters.command(["favorites", "favs"]))
    async def favs_cmd(c, m):
        uid, _ = who(m)
        rows = q("SELECT id, title FROM favs WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
        if not rows:
            return await m.reply("You have no favorites yet. Tap ⭐ on a now-playing card to add one.")
        kb = M([[B("▶️ " + t[:45], callback_data=f"mf:{i}")] for i, t in rows])
        await m.reply("⭐ <b>Your favorites</b> (tap to queue in this group):", parse_mode=HTML, reply_markup=kb)

    # ── buttons ──
    @bot.on_callback_query(filters.regex(r"^mp:"))
    async def player_cb(c, cq):
        action = cq.data.split(":")[1]
        chat_id = cq.message.chat.id
        st = S(chat_id)
        uid = cq.from_user.id
        if not st.current or cq.message.id != st.card_id:
            return await cq.answer("This player is outdated. Use /np.", show_alert=True)

        if action not in ("fav", "queue", "vol") and not await can_control(chat_id, uid):
            return await cq.answer(
                "Only admins or the person who requested this song can use these.", show_alert=True
            )

        try:
            if action == "pause":
                if st.paused:
                    await call.resume(chat_id)
                    st.anchor, st.paused = time.monotonic(), False
                else:
                    await call.pause(chat_id)
                    st.pos, st.anchor, st.paused = st.position(), None, True
                await refresh_card(chat_id)
            elif action == "back":
                await seek(chat_id, -10)
            elif action == "fwd":
                await seek(chat_id, 10)
            elif action == "replay":
                await start_track(chat_id, st.current, new_card=False)
            elif action == "skip":
                await advance(chat_id, natural=False)
            elif action == "loop":
                st.loop = {"off": "one", "one": "all", "all": "off"}[st.loop]
                await refresh_card(chat_id)
            elif action == "auto":
                st.autoplay = not st.autoplay
                await refresh_card(chat_id)
            elif action == "shuffle":
                random.shuffle(st.queue)
                await refresh_card(chat_id)
                return await cq.answer("🔀 Queue shuffled")
            elif action == "queue":
                if not st.queue:
                    return await cq.answer("Queue is empty.", show_alert=True)
                text = "\n".join(f"{i}. {t.title[:35]}" for i, t in enumerate(st.queue[:5], 1))
                if len(st.queue) > 5:
                    text += f"\n…+{len(st.queue) - 5} more"
                return await cq.answer(text[:190], show_alert=True)
            elif action == "end":
                await finish(chat_id)
                return await cq.answer("⏹ Stopped")
            elif action in ("vup", "vdn"):
                st.volume = max(10, min(200, st.volume + (10 if action == "vup" else -10)))
                await call.change_volume_call(chat_id, st.volume)
                await refresh_card(chat_id)
            elif action == "thumb":
                st.thumb_on = not st.thumb_on
                await send_card(chat_id)
                return await cq.answer(f"Thumbnail {'ON' if st.thumb_on else 'OFF'}")
            elif action == "close":
                if st.updater:
                    st.updater.cancel()
                st.card_id = None
                try:
                    await cq.message.delete()
                except Exception:
                    pass
                return await cq.answer("Player closed. Music keeps playing; /np brings it back.")
            elif action == "fav":
                q(
                    "INSERT OR IGNORE INTO favs(user_id, title, url) VALUES(?, ?, ?)",
                    (uid, st.current.title, st.current.page_url),
                )
                return await cq.answer("⭐ Added to your favorites (/favorites)")
        except Exception as e:
            return await cq.answer(f"Error: {e}"[:190], show_alert=True)
        await cq.answer()

    @bot.on_callback_query(filters.regex(r"^mf:"))
    async def fav_cb(c, cq):
        if cq.message.chat.type not in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            return await cq.answer("Use this inside a group.", show_alert=True)
        row = q("SELECT url, user_id FROM favs WHERE id=?", (int(cq.data.split(":")[1]),))
        if not row or row[0][1] != cq.from_user.id:
            return await cq.answer("That's not your favorites list.", show_alert=True)
        await cq.answer("Loading…")
        try:
            info = await asyncio.to_thread(extract, row[0][0])
            await ensure_assistant(cq.message.chat.id)
            await enqueue(cq.message.chat.id, info, cq.from_user.id, cq.from_user.first_name)
        except Exception as e:
            await cq.message.reply(f"❌ Couldn't play: {e}")
