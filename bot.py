import os
import json
import random
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import RetryAfter, BadRequest, Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# LOAD ENV
# =========================================================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

HOST_NAME = os.getenv("HOST_NAME", "POWER POINT BREAK")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@PowerPointBreak")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/PowerPointBreak")

ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "@MinexxProo")
DATA_FILE = os.getenv("DATA_FILE", "giveaway_data.json")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

# =========================================================
# GLOBALS / LOCK
# =========================================================
lock = asyncio.Lock()
admin_state = {}  # chat_id -> state

SPINNER = ["🔄", "🔃", "🔁", "🔂"]

LIVE_EDIT_INTERVAL = 5            # live post update every 5s
CLOSED_SPINNER_INTERVAL = 3       # closed spinner edit every 3s (safe)
MANUAL_DRAW_SECONDS = 40
MANUAL_DRAW_INTERVAL = 3          # reduce flood
AUTO_DRAW_SECONDS = 120           # 2 minutes
AUTO_DRAW_INTERVAL = 5            # every 5 seconds

# =========================================================
# DATA
# =========================================================
def fresh_default_data():
    return {
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        "start_time_ts": None,

        "live_message_id": None,
        "closed_message_id": None,
        "winners_message_id": None,

        "participants": {},  # uid(str) -> {"username": "@x", "name": ""}

        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},

        "first_winner_id": None,     # str uid
        "first_winner_username": "",
        "first_winner_name": "",

        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # claim window
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # auto winner feature
        "auto_winner_on": False,

        # history list
        # [{"ts": 170..., "title": "...", "prize":"...", "winners":[{"uid":"..","username":"..","first":bool}], "winner_count":10}]
        "history": [],
    }


def load_data():
    base = fresh_default_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}

    for k, v in base.items():
        d.setdefault(k, v)
    return d


async def save_data(data):
    async with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)


DATA = load_data()

# =========================================================
# HELPERS
# =========================================================
def is_admin_user(user_id: int) -> bool:
    return user_id == ADMIN_ID


def user_tag(username: str) -> str:
    if not username:
        return ""
    u = username.strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u


def participants_count() -> int:
    return len(DATA.get("participants", {}) or {})


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def format_hms(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"


def build_progress(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    blocks = 10
    filled = int(round(blocks * percent / 100))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty


def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    parts = t.split()
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0])

    if not parts or not parts[0].isdigit():
        return 0

    num = int(parts[0])
    unit = "".join(parts[1:])

    if unit.startswith("sec"):
        return num
    if unit.startswith("min"):
        return num * 60
    if unit.startswith("hour") or unit.startswith("hr"):
        return num * 3600

    return num


def format_rules() -> str:
    rules = (DATA.get("rules") or "").strip()
    if not rules:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts\n"
            "📌 Stay until winners announced"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    # Already emoji lines allowed
    return "\n".join(lines)


def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data="claim_prize")]]
    )


def winners_approve_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Post", callback_data="winners_approve"),
            InlineKeyboardButton("❌ Reject", callback_data="winners_reject"),
        ]]
    )


def preview_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✔️ Approve & Post", callback_data="preview_approve"),
                InlineKeyboardButton("❌ Reject Giveaway", callback_data="preview_reject"),
            ],
            [InlineKeyboardButton("✏️ Edit Again", callback_data="preview_edit")],
        ]
    )


def verify_add_more_done_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("➕ Add Another Link", callback_data="verify_add_more"),
            InlineKeyboardButton("✅ Done", callback_data="verify_add_done"),
        ]]
    )


def end_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm End", callback_data="end_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="end_cancel"),
        ]]
    )


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
        ]]
    )


def autowinner_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ ON", callback_data="autowin_on"),
            InlineKeyboardButton("❌ OFF", callback_data="autowin_off"),
        ]]
    )


def normalize_verify_ref(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return ""
    if s.startswith("-") and s[1:].isdigit():
        return s
    if s.startswith("@"):
        return s
    raw = s.replace(" ", "")
    if "t.me/" in raw:
        slug = raw.split("t.me/", 1)[1]
        slug = slug.split("?", 1)[0]
        slug = slug.split("/", 1)[0]
        if slug and not slug.startswith("+"):
            return user_tag(slug)
    return ""


def parse_user_lines(text: str):
    """
    Accept:
    123456789
    @name | 123456789
    """
    out = []
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    for line in lines:
        if "|" in line:
            left, right = line.split("|", 1)
            uname = user_tag(left.strip().lstrip("@"))
            uid = right.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, uname))
        else:
            uid = line.strip().replace(" ", "")
            if uid.isdigit():
                out.append((uid, ""))
    return out


async def safe_edit_text(bot, chat_id: int, message_id: int, text: str, reply_markup=None):
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(int(getattr(e, "retry_after", 3)) + 1)
        try:
            return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
        except Exception:
            return None
    except Exception:
        return None


async def safe_delete(bot, chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def safe_send(bot, chat_id: int, text: str, reply_markup=None):
    try:
        return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    except RetryAfter as e:
        await asyncio.sleep(int(getattr(e, "retry_after", 3)) + 1)
        try:
            return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
        except Exception:
            return None
    except Exception:
        return None


# =========================================================
# POPUP TEXTS (ALERT)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫 You have already won a previous giveaway.\n"
        "To keep the giveaway fair for everyone,\n"
        "repeat winners are restricted from participating.\n"
        "🙏 Please wait for the next Giveaway"
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "✨ CONGRATULATIONS! 🌟\n"
        "You joined the giveaway FIRST and secured the 🥇 1st Winner spot!\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📸 Screenshot & post in the group to confirm."
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY Unsuccessful\n"
        "You’ve already joined this giveaway 🎁\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result ⏳"
    )


def popup_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n"
        "You’ve successfully joined the giveaway ✅\n\n"
        "Your details:\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        "If you believe this is a mistake, contact admin:\n"
        f"`{ADMIN_CONTACT}`"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    prize = (DATA.get("prize") or "").strip() or "Prize"
    return (
        "🌟 CONGRATULATIONS! ✨\n\n"
        "You’re an official winner of this giveaway 🏆\n\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "🎁 PRIZE WON\n"
        f"🏆 {prize}\n\n"
        "📩 Claim your prize — contact admin:\n"
        f"`{ADMIN_CONTACT}`"
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


# =========================================================
# TEXT BUILDERS (CHANNEL / ADMIN)
# =========================================================
def build_preview_text() -> str:
    remaining = int(DATA.get("duration_seconds", 0) or 0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️🔥 {HOST_NAME} GIVEAWAY 🔥⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{DATA.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {DATA.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_progress(0)} 0%\n\n"
        "📜 RULES\n"
        f"{format_rules()}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = int(DATA.get("duration_seconds", 1) or 1)
    elapsed = max(0, duration - max(0, remaining))
    percent = int(round((elapsed / float(duration)) * 100)) if duration > 0 else 0
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️🔥 {HOST_NAME} GIVEAWAY 🔥⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{DATA.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {DATA.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_progress(percent)} {percent}%\n\n"
        "📜 RULES\n"
        f"{format_rules()}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


def build_closed_post_text(spin: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY HAS ENDED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway window is officially closed.\n"
        "🔒 All entries are now final and locked.\n\n"
        f"👥 Participants: {participants_count()}\n"
        f"🏆 Winners: {DATA.get('winner_count',0)}\n\n"
        "🎯 Winner selection is underway\n"
        f"{spin} Please stay tuned for the official announcement.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    prize = (DATA.get("prize") or "").strip() or "Prize"
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆✨ GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append("🎉 The wait is over!")
    lines.append("Here are the official winners of today’s giveaway 👇")
    lines.append("")
    lines.append("🎁 PRIZE WON")
    lines.append(f"🏆 {prize}")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    lines.append(f"👑 Username: {first_user or 'N/A'}")
    lines.append(f"🆔 User ID: {first_uid}")
    lines.append("⚡ Secured instantly by joining first")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1
    lines.append("")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("🔐 User ID based selection only.")
    lines.append("")
    lines.append("⏰ Important:")
    lines.append("🎁 Winners must claim their prize within 24 hours.")
    lines.append("❌ Unclaimed prizes will automatically expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: ⚡ {HOST_NAME}")
    lines.append("👇 Tap the button below to claim your prize 👇")
    return "\n".join(lines)


def build_manual_draw_progress(percent: int, spin: str) -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔎 Selecting winners... {percent}%\n"
        f"📊 Progress: {build_progress(percent)}\n\n"
        f"{spin} Winner selection is in progress\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only."
    )


# =========================================================
# VERIFY CHECK
# =========================================================
async def verify_user_join(bot, user_id: int) -> bool:
    targets = DATA.get("verify_targets", []) or []
    if not targets:
        return True

    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False
        try:
            member = await bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False

    return True


# =========================================================
# JOBS (LIVE / CLOSED / CLAIM / AUTO DRAW)
# =========================================================
async def live_tick(context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        if not DATA.get("active"):
            return

        start_ts = DATA.get("start_time_ts")
        if not start_ts:
            DATA["start_time_ts"] = now_ts()
            await save_data(DATA)
            start_ts = DATA["start_time_ts"]

        duration = int(DATA.get("duration_seconds", 1) or 1)
        elapsed = int(now_ts() - float(start_ts))
        remaining = duration - elapsed

        live_mid = DATA.get("live_message_id")

    if remaining <= 0:
        await close_giveaway_auto(context)
        return

    if not live_mid:
        return

    await safe_edit_text(
        context.bot,
        CHANNEL_ID,
        live_mid,
        build_live_text(remaining),
        reply_markup=join_button_markup()
    )


async def closed_spinner_tick(context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        mid = DATA.get("closed_message_id")
        if not mid:
            return
        if DATA.get("winners_message_id"):
            return
        tick = context.job.data.get("tick", 0) + 1
        context.job.data["tick"] = tick
        spin = SPINNER[(tick - 1) % len(SPINNER)]

    await safe_edit_text(context.bot, CHANNEL_ID, mid, build_closed_post_text(spin))


async def expire_claim_button(context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        mid = DATA.get("winners_message_id")
        exp = DATA.get("claim_expires_ts")
    if not mid or not exp:
        return

    if now_ts() < float(exp):
        return

    try:
        await context.bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=mid, reply_markup=None)
    except Exception:
        pass


async def remove_closed_message(context: ContextTypes.DEFAULT_TYPE):
    async with lock:
        mid = DATA.get("closed_message_id")
        DATA["closed_message_id"] = None
        await save_data(DATA)

    if mid:
        await safe_delete(context.bot, CHANNEL_ID, mid)


# =========================================================
# CORE ACTIONS
# =========================================================
async def close_giveaway_auto(context: ContextTypes.DEFAULT_TYPE):
    # stop live
    async with lock:
        DATA["active"] = False
        DATA["closed"] = True
        live_mid = DATA.get("live_message_id")
        DATA["live_message_id"] = None
        await save_data(DATA)

    if live_mid:
        await safe_delete(context.bot, CHANNEL_ID, live_mid)

    # post closed msg
    m = await safe_send(context.bot, CHANNEL_ID, build_closed_post_text("🔄"))
    if m:
        async with lock:
            DATA["closed_message_id"] = m.message_id
            await save_data(DATA)

        # start spinner job
        context.job_queue.run_repeating(
            closed_spinner_tick,
            interval=CLOSED_SPINNER_INTERVAL,
            first=0,
            data={"tick": 0},
            name="closed_spinner"
        )

    # admin notify depending on auto winner
    async with lock:
        auto_on = bool(DATA.get("auto_winner_on"))
        title = DATA.get("title", "")
        parts = participants_count()

    if auto_on:
        await safe_send(
            context.bot,
            ADMIN_ID,
            "⏰ Giveaway Closed!\n✅ Auto winner is ON\n\n"
            f"Giveaway: {title}\n"
            f"Total Participants: {parts}\n\n"
            "Auto winner selection will start now."
        )
        # Start auto draw (2 minutes progress, then auto post winners)
        context.job_queue.run_once(auto_draw_start, when=1)
    else:
        await safe_send(
            context.bot,
            ADMIN_ID,
            "⏰ Giveaway Closed!\nAuto winner is OFF ❌\n\nNow use /draw to select winners."
        )


async def pick_winners() -> tuple[str, str, list]:
    # returns first_uid, first_uname, random_list[(uid, uname)]
    async with lock:
        participants = DATA.get("participants", {}) or {}
        if not participants:
            return "", "", []

        winner_count = max(1, int(DATA.get("winner_count", 1) or 1))

        first_uid = DATA.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            DATA["first_winner_id"] = first_uid
            DATA["first_winner_username"] = info.get("username", "")
            DATA["first_winner_name"] = info.get("name", "")

        first_uname = DATA.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

        pool = [uid for uid in participants.keys() if uid != first_uid]
        need = max(0, winner_count - 1)
        need = min(need, len(pool))
        selected = random.sample(pool, need) if need > 0 else []

        random_list = []
        for uid in selected:
            info = participants.get(uid, {}) or {}
            random_list.append((uid, info.get("username", "")))

        # winners map
        winners_map = {first_uid: {"username": first_uname}}
        for uid, uname in random_list:
            winners_map[uid] = {"username": uname}

        DATA["winners"] = winners_map

        # pending winners text
        pending = build_winners_post_text(first_uid, first_uname, random_list)
        DATA["pending_winners_text"] = pending
        await save_data(DATA)

        return first_uid, first_uname, random_list


async def post_winners_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    # remove closed msg first
    async with lock:
        closed_mid = DATA.get("closed_message_id")

    if closed_mid:
        await safe_delete(context.bot, CHANNEL_ID, closed_mid)
        async with lock:
            DATA["closed_message_id"] = None
            await save_data(DATA)

    # delete previous winners post (if exists)
    async with lock:
        old_winners_mid = DATA.get("winners_message_id")

    if old_winners_mid:
        await safe_delete(context.bot, CHANNEL_ID, old_winners_mid)

    m = await safe_send(context.bot, CHANNEL_ID, text, reply_markup=claim_button_markup())
    if not m:
        return False

    async with lock:
        DATA["winners_message_id"] = m.message_id
        # claim window 24h
        ts = now_ts()
        DATA["claim_start_ts"] = ts
        DATA["claim_expires_ts"] = ts + 24 * 3600

        # save to history
        winners_map = DATA.get("winners", {}) or {}
        history_item = {
            "ts": ts,
            "title": DATA.get("title", ""),
            "prize": DATA.get("prize", ""),
            "winner_count": DATA.get("winner_count", 0),
            "winners": [
                {"uid": uid, "username": (winners_map.get(uid, {}) or {}).get("username", ""), "first": (uid == DATA.get("first_winner_id"))}
                for uid in winners_map.keys()
            ]
        }
        DATA["history"].insert(0, history_item)
        await save_data(DATA)

    # schedule claim expiry remove button
    context.job_queue.run_once(expire_claim_button, when=24 * 3600, name="claim_expire")
    return True


# =========================================================
# AUTO DRAW (2 minutes, %+bar, every 5s)
# =========================================================
async def auto_draw_start(context: ContextTypes.DEFAULT_TYPE):
    # if already winners posted stop
    async with lock:
        if DATA.get("winners_message_id"):
            return

    # run progress job (admin message)
    msg = await safe_send(context.bot, ADMIN_ID, build_manual_draw_progress(0, "🔄"))
    if not msg:
        return

    # store in job data
    data = {
        "admin_msg_id": msg.message_id,
        "start": now_ts(),
        "tick": 0
    }
    context.job_queue.run_repeating(
        auto_draw_tick,
        interval=AUTO_DRAW_INTERVAL,
        first=0,
        data=data,
        name="auto_draw_tick"
    )
    context.job_queue.run_once(
        auto_draw_finalize,
        when=AUTO_DRAW_SECONDS,
        data=data,
        name="auto_draw_finalize"
    )


async def auto_draw_tick(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    d["tick"] = d.get("tick", 0) + 1
    tick = d["tick"]
    spin = SPINNER[(tick - 1) % len(SPINNER)]

    # percent based on time (safe)
    elapsed = now_ts() - float(d.get("start", now_ts()))
    percent = int(min(100, round((elapsed / float(AUTO_DRAW_SECONDS)) * 100)))

    await safe_edit_text(context.bot, ADMIN_ID, d["admin_msg_id"], build_manual_draw_progress(percent, spin))


async def auto_draw_finalize(context: ContextTypes.DEFAULT_TYPE):
    # stop tick job (best effort)
    try:
        for j in context.job_queue.jobs():
            if j.name == "auto_draw_tick":
                j.schedule_removal()
    except Exception:
        pass

    # pick + post winners
    first_uid, first_uname, random_list = await pick_winners()
    async with lock:
        text = (DATA.get("pending_winners_text") or "").strip()

    if not text:
        await safe_send(context.bot, ADMIN_ID, "No participants to draw winners from.")
        return

    # remove closed msg after 2 minutes (your rule)
    await remove_closed_message(context)

    ok = await post_winners_to_channel(context, text)
    if ok:
        await safe_send(context.bot, ADMIN_ID, "✅ Auto winner posted to channel successfully!")
    else:
        await safe_send(context.bot, ADMIN_ID, "❌ Auto winner failed to post (check bot admin permissions).")


# =========================================================
# COMMANDS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and is_admin_user(u.id):
        await update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    await update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL — POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n"
        "/autowinnerpost\n"
        "/winnerlist\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "♻️ RESET\n"
        "/reset"
    )


async def cmd_autowinnerpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ AUTO WINNER POST SYSTEM\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choose:",
        reply_markup=autowinner_markup()
    )


async def cmd_addverifylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    admin_state[update.effective_chat.id] = "add_verify"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak\n\n"
        "After adding, users must join ALL verify targets to join giveaway."
    )


async def cmd_removeverifylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    targets = DATA.get("verify_targets", []) or []
    if not targets:
        await update.message.reply_text("No verify targets are set.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "🗑 REMOVE VERIFY TARGET",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "Current Verify Targets:",
        "",
    ]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += ["", "Send a number to remove that target.", "11) Remove ALL verify targets"]
    admin_state[update.effective_chat.id] = "remove_verify_pick"
    await update.message.reply_text("\n".join(lines))


async def cmd_newgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return

    # stop any jobs by names (best effort)
    for j in context.job_queue.jobs():
        if j.name in ("live_tick", "closed_spinner", "auto_draw_tick", "auto_draw_finalize"):
            j.schedule_removal()

    async with lock:
        # keep perma + verify + history (you can remove if you want)
        keep_perma = DATA.get("permanent_block", {})
        keep_verify = DATA.get("verify_targets", [])
        keep_history = DATA.get("history", [])

        DATA.clear()
        DATA.update(fresh_default_data())
        DATA["permanent_block"] = keep_perma
        DATA["verify_targets"] = keep_verify
        DATA["history"] = keep_history
        await save_data(DATA)

    admin_state[update.effective_chat.id] = "title"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


async def cmd_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    parts = DATA.get("participants", {}) or {}
    if not parts:
        await update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Total Participants: {len(parts)}",
        "",
    ]
    i = 1
    for uid, info in parts.items():
        uname = (info or {}).get("username", "")
        if uname:
            lines.append(f"{i}. {uname} | User ID: {uid}")
        else:
            lines.append(f"{i}. User ID: {uid}")
        i += 1

    await update.message.reply_text("\n".join(lines))


async def cmd_endgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    if not DATA.get("active"):
        await update.message.reply_text("No active giveaway is running right now.")
        return

    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure you want to end this giveaway now?\n\n"
        "✅ Confirm End → Giveaway will close\n"
        "❌ Cancel → Giveaway will continue",
        reply_markup=end_confirm_markup()
    )


async def cmd_draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    if not DATA.get("closed"):
        await update.message.reply_text("Giveaway is not closed yet or no giveaway running.")
        return
    if not (DATA.get("participants", {}) or {}):
        await update.message.reply_text("No participants to draw winners from.")
        return

    # Manual draw progress message
    msg = await safe_send(context.bot, update.effective_chat.id, build_manual_draw_progress(0, "🔄"))
    if not msg:
        return

    ctx = {"msg_id": msg.message_id, "start": now_ts(), "tick": 0}

    async def manual_tick(job_context: ContextTypes.DEFAULT_TYPE):
        ctx["tick"] += 1
        spin = SPINNER[(ctx["tick"] - 1) % len(SPINNER)]
        elapsed = now_ts() - float(ctx["start"])
        percent = int(min(100, round((elapsed / float(MANUAL_DRAW_SECONDS)) * 100)))
        await safe_edit_text(job_context.bot, update.effective_chat.id, ctx["msg_id"], build_manual_draw_progress(percent, spin))

    async def manual_finalize(job_context: ContextTypes.DEFAULT_TYPE):
        # stop repeating jobs
        try:
            for j in job_context.job_queue.jobs():
                if j.name == f"manual_draw_tick_{ctx['msg_id']}":
                    j.schedule_removal()
        except Exception:
            pass

        await pick_winners()
        async with lock:
            text = (DATA.get("pending_winners_text") or "").strip()

        await safe_edit_text(
            job_context.bot,
            update.effective_chat.id,
            ctx["msg_id"],
            text,
            reply_markup=winners_approve_markup()
        )

    context.job_queue.run_repeating(
        manual_tick,
        interval=MANUAL_DRAW_INTERVAL,
        first=0,
        name=f"manual_draw_tick_{ctx['msg_id']}"
    )
    context.job_queue.run_once(
        manual_finalize,
        when=MANUAL_DRAW_SECONDS,
        name=f"manual_draw_finalize_{ctx['msg_id']}"
    )


async def cmd_blockpermanent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    admin_state[update.effective_chat.id] = "perma_block_list"
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )
    admin_state[update.effective_chat.id] = "unban_choose"
    await update.message.reply_text("Choose Unban Type:", reply_markup=kb)


async def cmd_removeban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )
    admin_state[update.effective_chat.id] = "removeban_choose"
    await update.message.reply_text("Choose which ban list to reset:", reply_markup=kb)


async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return

    perma = DATA.get("permanent_block", {}) or {}
    oldw = DATA.get("old_winners", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {DATA.get('old_winner_mode','skip').upper()}")
    lines.append("")
    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}User ID: {uid}")
            i += 1
    else:
        lines.append("No old winner blocked users.")
    lines.append("")
    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {uname+' | ' if uname else ''}User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    await update.message.reply_text("\n".join(lines))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ RESET CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "This will remove EVERYTHING (all data).\n"
        "Are you sure?",
        reply_markup=reset_confirm_markup()
    )


async def cmd_winnerlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_user(update.effective_user.id):
        return
    history = DATA.get("history", []) or []
    if not history:
        await update.message.reply_text("No winner history found yet.")
        return

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY — POWER POINT BREAK")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    show = history[:10]  # last 10
    for item in show:
        ts = float(item.get("ts", 0) or 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        dt_str = dt.strftime("%d %b %Y • %I:%M %p")
        prize = item.get("prize", "") or "Prize"
        lines.append(f"📅 {dt_str}")
        lines.append(f"🎁 Prize: {prize}")
        lines.append("")
        winners = item.get("winners", []) or []
        first = next((w for w in winners if w.get("first")), None)
        if first:
            lines.append("🥇 First Join Champion")
            lines.append(f"👤 {first.get('username','') or 'N/A'} | 🆔 {first.get('uid','')}")
            lines.append("")
        lines.append("👑 Other Winners")
        i = 1
        for w in winners:
            if w.get("first"):
                continue
            uid = w.get("uid", "")
            uname = w.get("username", "")
            if uname:
                lines.append(f"{i}) 👤 {uname} | 🆔 {uid}")
            else:
                lines.append(f"{i}) 👤 User ID: {uid}")
            i += 1
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")

    await update.message.reply_text("\n".join(lines))


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin_user(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    state = admin_state.get(chat_id)
    if not state:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    # ADD VERIFY
    if state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            await update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        async with lock:
            targets = DATA.get("verify_targets", []) or []
            if len(targets) >= 100:
                await update.message.reply_text("Max verify targets reached (100). Remove some first.")
                return
            targets.append({"ref": ref, "display": ref})
            DATA["verify_targets"] = targets
            await save_data(DATA)

        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(DATA.get('verify_targets', []) or [])}\n\n"
            "What do you want to do next?",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # REMOVE VERIFY PICK
    if state == "remove_verify_pick":
        if not msg.isdigit():
            await update.message.reply_text("Send a valid number (1,2,3... or 11).")
            return
        n = int(msg)
        async with lock:
            targets = DATA.get("verify_targets", []) or []
            if not targets:
                admin_state.pop(chat_id, None)
                await update.message.reply_text("No verify targets remain.")
                return

            if n == 11:
                DATA["verify_targets"] = []
                await save_data(DATA)
                admin_state.pop(chat_id, None)
                await update.message.reply_text("✅ All verify targets removed successfully!")
                return

            if n < 1 or n > len(targets):
                await update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            DATA["verify_targets"] = targets
            await save_data(DATA)

        admin_state.pop(chat_id, None)
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(DATA.get('verify_targets', []) or [])}"
        )
        return

    # GIVEAWAY SETUP
    if state == "title":
        async with lock:
            DATA["title"] = msg
            await save_data(DATA)
        admin_state[chat_id] = "prize"
        await update.message.reply_text("✅ Title saved!\n\nNow send Giveaway Prize (multi-line allowed):")
        return

    if state == "prize":
        async with lock:
            DATA["prize"] = msg
            await save_data(DATA)
        admin_state[chat_id] = "winners"
        await update.message.reply_text("✅ Prize saved!\n\nNow send Total Winner Count (1 - 1000000):")
        return

    if state == "winners":
        if not msg.isdigit():
            await update.message.reply_text("Please send a valid number for winner count.")
            return
        count = max(1, min(1000000, int(msg)))
        async with lock:
            DATA["winner_count"] = count
            await save_data(DATA)
        admin_state[chat_id] = "duration"
        await update.message.reply_text(
            "✅ Winner count saved!\n\n"
            f"🏆 Total Winners: {count}\n\n"
            "⏱ Send Giveaway Duration\n"
            "Example:\n"
            "30 Second\n"
            "30 Minute\n"
            "11 Hour"
        )
        return

    if state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            await update.message.reply_text("Invalid duration. Example: 30 Second / 30 Minute / 11 Hour")
            return
        async with lock:
            DATA["duration_seconds"] = seconds
            await save_data(DATA)
        admin_state[chat_id] = "old_winner_mode"
        await update.message.reply_text(
            "🔐 OLD WINNER PROTECTION MODE\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n"
            "• Old winners will ALSO be included in winner selection\n\n"
            "Reply with:\n"
            "1 → BLOCK\n"
            "2 → SKIP"
        )
        return

    if state == "old_winner_mode":
        if msg not in ("1", "2"):
            await update.message.reply_text("Reply with 1 or 2 only.")
            return

        if msg == "2":
            async with lock:
                DATA["old_winner_mode"] = "skip"
                DATA["old_winners"] = {}
                await save_data(DATA)
            admin_state[chat_id] = "rules"
            await update.message.reply_text(
                "📌 Old Winner Mode set to: SKIP ✅\n\n"
                "Now send Giveaway Rules (multi-line):"
            )
            return

        async with lock:
            DATA["old_winner_mode"] = "block"
            DATA["old_winners"] = {}
            await save_data(DATA)

        admin_state[chat_id] = "old_winner_block_list"
        await update.message.reply_text(
            "⛔ OLD WINNER BLOCK LIST SETUP\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "556677"
        )
        return

    if state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        async with lock:
            ow = DATA.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            DATA["old_winners"] = ow
            await save_data(DATA)
        admin_state[chat_id] = "rules"
        await update.message.reply_text(
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            f"📌 Total Added: {len(DATA['old_winners']) - before}\n\n"
            "Now send Giveaway Rules (multi-line):"
        )
        return

    if state == "rules":
        async with lock:
            DATA["rules"] = msg
            await save_data(DATA)
        admin_state.pop(chat_id, None)
        await update.message.reply_text("✅ Rules saved!\nShowing preview…")
        await update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMA BLOCK
    if state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        async with lock:
            perma = DATA.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[uid] = {"username": uname}
            DATA["permanent_block"] = perma
            await save_data(DATA)
        admin_state.pop(chat_id, None)
        await update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(DATA['permanent_block']) - before}\n"
            f"Total Blocked: {len(DATA['permanent_block'])}"
        )
        return

    # UNBAN INPUT
    if state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        async with lock:
            perma = DATA.get("permanent_block", {}) or {}
            if uid in perma:
                del perma[uid]
                DATA["permanent_block"] = perma
                await save_data(DATA)
                await update.message.reply_text("✅ Unbanned from Permanent Block successfully!")
            else:
                await update.message.reply_text("This user id is not in Permanent Block list.")
        admin_state.pop(chat_id, None)
        return

    if state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        async with lock:
            ow = DATA.get("old_winners", {}) or {}
            if uid in ow:
                del ow[uid]
                DATA["old_winners"] = ow
                await save_data(DATA)
                await update.message.reply_text("✅ Unbanned from Old Winner Block successfully!")
            else:
                await update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state.pop(chat_id, None)
        return


# =========================================================
# CALLBACKS
# =========================================================
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    data_q = query.data
    uid = str(query.from_user.id)

    # auto winner buttons
    if data_q in ("autowin_on", "autowin_off"):
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        async with lock:
            DATA["auto_winner_on"] = (data_q == "autowin_on")
            await save_data(DATA)
        await query.answer()
        if data_q == "autowin_on":
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "✅ Auto Winner is now ON ✅\n⏳ When giveaway ends, winners will be selected automatically.")
        else:
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "✅ Auto Winner is now OFF ❌\n⏰ Giveaway Closed! Auto winner is OFF ❌\nNow use /draw to select winners.")
        return

    # verify add more/done
    if data_q == "verify_add_more":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        admin_state[query.message.chat_id] = "add_verify"
        await query.answer()
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id, "Send another Chat ID or @username:")
        return

    if data_q == "verify_add_done":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        admin_state.pop(query.message.chat_id, None)
        await query.answer()
        await safe_edit_text(
            context.bot,
            query.message.chat_id,
            query.message.message_id,
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY SETUP COMPLETED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Total Verify Targets: {len(DATA.get('verify_targets', []) or [])}\n"
            "All users must join ALL targets to join giveaway."
        )
        return

    # preview actions
    if data_q.startswith("preview_"):
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return

        if data_q == "preview_approve":
            await query.answer()

            duration = int(DATA.get("duration_seconds", 0) or 1)
            m = await safe_send(context.bot, CHANNEL_ID, build_live_text(duration), reply_markup=join_button_markup())
            if not m:
                await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                     "❌ Failed to post in channel. Make sure bot is admin and CHANNEL_ID correct.")
                return

            async with lock:
                DATA["live_message_id"] = m.message_id
                DATA["active"] = True
                DATA["closed"] = False
                DATA["start_time_ts"] = now_ts()
                DATA["closed_message_id"] = None
                DATA["winners_message_id"] = None

                DATA["participants"] = {}
                DATA["winners"] = {}
                DATA["pending_winners_text"] = ""
                DATA["first_winner_id"] = None
                DATA["first_winner_username"] = ""
                DATA["first_winner_name"] = ""
                DATA["claim_start_ts"] = None
                DATA["claim_expires_ts"] = None

                await save_data(DATA)

            # start live ticking
            context.job_queue.run_repeating(live_tick, interval=LIVE_EDIT_INTERVAL, first=0, name="live_tick")

            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "✅ Giveaway approved and posted to channel!")
            return

        if data_q == "preview_reject":
            await query.answer()
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "❌ Giveaway rejected and cleared.")
            return

        if data_q == "preview_edit":
            await query.answer()
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "✏️ Edit Mode\n\nStart again with /newgiveaway")
            return

    # end giveaway confirm/cancel
    if data_q == "end_confirm":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        await close_giveaway_auto(context)
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "✅ Giveaway Closed Successfully!")
        return

    if data_q == "end_cancel":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "❌ Cancelled. Giveaway is still running.")
        return

    # reset confirm/cancel
    if data_q == "reset_cancel":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id, "❌ Reset cancelled.")
        return

    if data_q == "reset_confirm":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()

        # show progress 40s, update every 5s
        msg_id = query.message.message_id
        start = now_ts()

        for tick in range(1, 9):  # 8 updates -> 40s
            elapsed = now_ts() - start
            percent = int(min(100, round((elapsed / 40.0) * 100)))
            spin = SPINNER[(tick - 1) % len(SPINNER)]
            txt = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "♻️ RESET IN PROGRESS\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔎 Cleaning system... {percent}%\n"
                f"📊 Progress: {build_progress(percent)}\n\n"
                f"{spin} Please wait"
            )
            await safe_edit_text(context.bot, query.message.chat_id, msg_id, txt)
            await asyncio.sleep(5)

        # delete channel messages (best effort)
        async with lock:
            live_mid = DATA.get("live_message_id")
            closed_mid = DATA.get("closed_message_id")
            winners_mid = DATA.get("winners_message_id")

        if live_mid:
            await safe_delete(context.bot, CHANNEL_ID, live_mid)
        if closed_mid:
            await safe_delete(context.bot, CHANNEL_ID, closed_mid)
        if winners_mid:
            await safe_delete(context.bot, CHANNEL_ID, winners_mid)

        # stop jobs
        for j in context.job_queue.jobs():
            if j.name in ("live_tick", "closed_spinner", "auto_draw_tick", "auto_draw_finalize"):
                j.schedule_removal()

        # full reset ALL
        async with lock:
            DATA.clear()
            DATA.update(fresh_default_data())
            await save_data(DATA)

        await safe_edit_text(
            context.bot,
            query.message.chat_id,
            msg_id,
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ RESET COMPLETED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Start again with:\n"
            "/newgiveaway"
        )
        return

    # unban choose
    if data_q == "unban_permanent":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        admin_state[query.message.chat_id] = "unban_permanent_input"
        await query.answer()
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "Send User ID (or @name | id) to unban from Permanent Block:")
        return

    if data_q == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        admin_state[query.message.chat_id] = "unban_oldwinner_input"
        await query.answer()
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "Send User ID (or @name | id) to unban from Old Winner Block:")
        return

    # removeban choose confirm
    if data_q in ("reset_permanent_ban", "reset_oldwinner_ban"):
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()

        if data_q == "reset_permanent_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "Confirm reset Permanent Ban List?", reply_markup=kb)
            return

        if data_q == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "Confirm reset Old Winner Ban List?", reply_markup=kb)
            return

    if data_q == "cancel_reset_ban":
        await query.answer()
        admin_state.pop(query.message.chat_id, None)
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id, "Cancelled.")
        return

    if data_q == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        async with lock:
            DATA["permanent_block"] = {}
            await save_data(DATA)
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "✅ Permanent Ban List has been reset.")
        return

    if data_q == "confirm_reset_oldwinner":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        async with lock:
            DATA["old_winners"] = {}
            await save_data(DATA)
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "✅ Old Winner Ban List has been reset.")
        return

    # join giveaway
    if data_q == "join_giveaway":
        if not DATA.get("active"):
            await query.answer("This giveaway is not active right now.", show_alert=True)
            return

        # verify targets
        ok = await verify_user_join(context.bot, int(uid))
        if not ok:
            await query.answer(popup_verify_required(), show_alert=True)
            return

        # permanent block
        if uid in (DATA.get("permanent_block", {}) or {}):
            await query.answer(popup_permanent_blocked(), show_alert=True)
            return

        # old winner block
        if DATA.get("old_winner_mode") == "block":
            if uid in (DATA.get("old_winners", {}) or {}):
                await query.answer(popup_old_winner_blocked(), show_alert=True)
                return

        # FIRST WINNER repeat popup ALWAYS
        first_uid = DATA.get("first_winner_id")
        if first_uid and uid == str(first_uid):
            uname = user_tag(query.from_user.username or "") or (DATA.get("first_winner_username") or "@username")
            await query.answer(popup_first_winner(uname, uid), show_alert=True)
            return

        # already joined
        if uid in (DATA.get("participants", {}) or {}):
            await query.answer(popup_already_joined(), show_alert=True)
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        async with lock:
            if not DATA.get("first_winner_id"):
                DATA["first_winner_id"] = uid
                DATA["first_winner_username"] = uname
                DATA["first_winner_name"] = full_name

            DATA["participants"][uid] = {"username": uname, "name": full_name}
            await save_data(DATA)

        # update live post once (safe)
        async with lock:
            live_mid = DATA.get("live_message_id")
            start_ts = DATA.get("start_time_ts")
            duration = int(DATA.get("duration_seconds", 1) or 1)

        if live_mid and start_ts:
            elapsed = int(now_ts() - float(start_ts))
            remaining = max(0, duration - elapsed)
            await safe_edit_text(context.bot, CHANNEL_ID, live_mid, build_live_text(remaining), reply_markup=join_button_markup())

        # show popup
        async with lock:
            if DATA.get("first_winner_id") == uid:
                await query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
            else:
                await query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
        return

    # winners approve/reject
    if data_q == "winners_approve":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()

        text = (DATA.get("pending_winners_text") or "").strip()
        if not text:
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "No pending winners preview found.")
            return

        ok = await post_winners_to_channel(context, text)
        if ok:
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "✅ Approved! Winners list posted to channel (with Claim button).")
        else:
            await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                                 "❌ Failed to post winners in channel (check bot admin permission / flood).")
        return

    if data_q == "winners_reject":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        async with lock:
            DATA["pending_winners_text"] = ""
            await save_data(DATA)
        await safe_edit_text(context.bot, query.message.chat_id, query.message.message_id,
                             "❌ Rejected! Winners list will NOT be posted.")
        return

    # claim prize
    if data_q == "claim_prize":
        winners = DATA.get("winners", {}) or {}
        if uid not in winners:
            await query.answer(popup_claim_not_winner(), show_alert=True)
            return

        exp = DATA.get("claim_expires_ts")
        if exp and now_ts() > float(exp):
            await query.answer(popup_prize_expired(), show_alert=True)
            return

        uname = winners.get(uid, {}).get("username", "") or user_tag(query.from_user.username or "") or "@username"
        await query.answer(popup_claim_winner(uname, uid), show_alert=True)
        return

    await query.answer()


# =========================================================
# MAIN
# =========================================================
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    app.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    app.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    app.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    app.add_handler(CommandHandler("participants", cmd_participants))
    app.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    app.add_handler(CommandHandler("draw", cmd_draw))

    app.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("removeban", cmd_removeban))
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))

    app.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # message flow
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_handler))
    app.add_handler(CallbackQueryHandler(cb_handler))

    return app


async def resume_jobs(app):
    # resume live tick if active
    if DATA.get("active"):
        app.job_queue.run_repeating(live_tick, interval=LIVE_EDIT_INTERVAL, first=0, name="live_tick")

    # resume closed spinner if closed and no winners yet
    if DATA.get("closed") and DATA.get("closed_message_id") and not DATA.get("winners_message_id"):
        app.job_queue.run_repeating(
            closed_spinner_tick,
            interval=CLOSED_SPINNER_INTERVAL,
            first=0,
            data={"tick": 0},
            name="closed_spinner"
        )

    # resume claim expiry if winners exist
    if DATA.get("winners_message_id") and DATA.get("claim_expires_ts"):
        remain = float(DATA["claim_expires_ts"]) - now_ts()
        if remain > 0:
            app.job_queue.run_once(expire_claim_button, when=remain, name="claim_expire")


def main():
    app = build_app()

    async def post_init(application):
        await resume_jobs(application)

    app.post_init = post_init

    print("Bot is running (PTB v20 compatible, GSM hosting ready) ...")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
