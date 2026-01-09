import os
import json
import random
import threading
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
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

# Auto winner timings
AUTO_DRAW_SECONDS = int(os.getenv("AUTO_DRAW_SECONDS", "120"))   # 2 minutes
MANUAL_DRAW_SECONDS = int(os.getenv("MANUAL_DRAW_SECONDS", "120"))
DRAW_TICK_SECONDS = int(os.getenv("DRAW_TICK_SECONDS", "5"))     # every 5 sec

RESET_SECONDS = int(os.getenv("RESET_SECONDS", "40"))
RESET_TICK_SECONDS = int(os.getenv("RESET_TICK_SECONDS", "5"))

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

# jobs
live_job = None
closed_spin_job = None
draw_job = None
draw_finalize_job = None
reset_job = None
reset_finalize_job = None

# =========================================================
# UI HELPERS (SAFE TELEGRAM BORDER)
# =========================================================
LINE = "━━━━━━━━━━━━━━━━━━━━"  # safe
LINE2 = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"  # also safe
SPIN = ["🔄", "🔃", "🔁", "🔂"]

# =========================================================
# DATA / STORAGE
# =========================================================
def fresh_default_data():
    return {
        "active": False,
        "closed": False,

        "title": "",
        "prize": "",               # keep as admin wrote
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",

        "start_ts": None,
        "live_message_id": None,      # giveaway live post message id
        "closed_message_id": None,    # giveaway ended post message id
        "winners_message_id": None,   # winners announcement post message id

        "participants": {},  # uid -> {"username": "@x", "name": ""}

        "verify_targets": [],     # [{"ref":"-100.. or @xx", "display":"..."}]
        "permanent_block": {},    # uid -> {"username":"@x"}

        "old_winner_mode": "skip",  # "skip" or "block"
        "old_winners": {},          # uid -> {"username":"@x"}

        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        "winners": {},               # uid -> {"username":"@x"}
        "pending_winners_text": "",  # admin preview winners text

        # claim window
        "claim_start_ts": None,
        "claim_expires_ts": None,  # ts + 24h

        # auto winner toggle
        "auto_winner": False,

        # history
        "winner_history": [],  # list of {ts, title, prize, uid, username}
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


def save_data():
    with lock:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


data = load_data()

# =========================================================
# GENERIC HELPERS
# =========================================================
def is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)


def user_tag(username: str) -> str:
    if not username:
        return ""
    u = username.strip()
    if not u:
        return ""
    return u if u.startswith("@") else "@" + u


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def dt_text(ts: float) -> str:
    # Day/Month/Year + time
    try:
        d = datetime.utcfromtimestamp(float(ts))
        return d.strftime("%d/%m/%Y %H:%M UTC")
    except Exception:
        return "Unknown"


def participants_count() -> int:
    return len(data.get("participants", {}) or {})


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"


def build_progress(percent: int) -> str:
    percent = max(0, min(100, int(percent)))
    blocks = 10
    filled = int(round(blocks * percent / 100.0))
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
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts\n"
            "📌 Stay until winners announced"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    # keep emoji if user wrote; else add bullets
    return "\n".join(lines)


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


async def verify_user_join(bot, user_id: int) -> bool:
    targets = data.get("verify_targets", []) or []
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
# MARKUPS
# =========================================================
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


def autowinner_markup():
    on = "✅ ON" if data.get("auto_winner") else "ON"
    off = "✅ OFF" if not data.get("auto_winner") else "OFF"
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(on, callback_data="autowinner_on"),
            InlineKeyboardButton(off, callback_data="autowinner_off"),
        ]]
    )


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ CONFIRM RESET", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ REJECT", callback_data="reset_reject"),
        ]]
    )


# =========================================================
# POPUP / DM TEXTS
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
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "🌟 CONGRATULATIONS! ✨\n"
        "You joined the giveaway FIRST and secured the 🥇 1st Winner spot!\n\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📸 Screenshot & post in the group to confirm."
    )


def popup_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway ❤️‍🩹"
    )


def dm_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        "If you believe this is a mistake, contact admin:\n"
        f"{ADMIN_CONTACT}\n\n"
        f"— {HOST_NAME}"
    )


def dm_claim_winner(username: str, uid: str) -> str:
    prize = (data.get("prize") or "").strip() or "Prize"
    return (
        "🌟 CONGRATULATIONS! ✨\n\n"
        "You’re an official winner of this giveaway 🏆\n\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "🎁 PRIZE WON\n"
        f"🏆 {prize}\n\n"
        "📩 Claim your prize — contact admin:\n"
        f"{ADMIN_CONTACT}\n\n"
        f"— {HOST_NAME}"
    )


# =========================================================
# TELEGRAM STYLE TEXT BUILDERS (YOUR STYLE)
# =========================================================
def build_preview_text() -> str:
    # preview uses same style as live (0 participants)
    remaining = int(data.get("duration_seconds", 0) or 0)
    return build_live_text(remaining, preview=True)


def build_live_text(remaining: int, preview: bool = False) -> str:
    duration = int(data.get("duration_seconds", 1) or 1)
    elapsed = max(0, min(duration, duration - max(0, remaining)))
    percent = int(round((elapsed / float(duration)) * 100)) if duration > 0 else 0

    # IMPORTANT: your display wants "00 : 03 : 00" style
    # remaining is seconds -> show h : m : s
    time_str = format_hms(remaining)

    prize = (data.get("prize") or "").strip()
    if not prize:
        prize = "ChatGPT PREMIUM"

    rules = format_rules()

    # progress line format: bar + percent
    bar = build_progress(percent)

    title = (data.get("title") or "").strip()
    if not title:
        title = "⚡️🔥 POWER POINT BREAK GIVEAWAY 🔥⚡️"

    # EXACT STYLE (Safe border)
    return (
        f"{LINE}\n"
        f"⚡️🔥 {title} 🔥⚡️\n"
        f"{LINE}\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{prize}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count() if not preview else 0}\n"
        f"🏅 TOTAL WINNERS: {int(data.get('winner_count', 0) or 0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {time_str}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "📜 RULES\n"
        f"{rules}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


def build_giveaway_ended_text() -> str:
    return (
        f"{LINE}\n"
        "🚫 GIVEAWAY HAS ENDED 🚫\n"
        f"{LINE}\n\n"
        "⏰ The giveaway window is officially closed.\n"
        "🔒 All entries are now final and locked.\n\n"
        f"👥 Participants: {participants_count()}\n"
        f"🏆 Winners: {int(data.get('winner_count', 0) or 0)}\n\n"
        "🎯 Winner selection is underway\n"
        "🔄 Please stay tuned for the official announcement.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def build_draw_progress_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        f"{LINE2}\n"
        "🎲 RANDOM WINNER SELECTION\n"
        f"{LINE2}\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        f"📊 Progress\n{bar} {percent}%\n\n"
        "✅ 100% Random & Fair\n"
        "🔐 User ID based selection only."
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    prize = (data.get("prize") or "").strip() or "Prize"

    lines = []
    lines.append(f"{LINE}")
    lines.append("🏆✨ GIVEAWAY WINNERS ANNOUNCEMENT ✨🏆")
    lines.append(f"{LINE}")
    lines.append("🎉 The wait is over!")
    lines.append("Here are the official winners of today’s giveaway 👇")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        lines.append(f"👑 Username: {first_user}")
    else:
        lines.append("👑 Username: (No username)")
    lines.append(f"🆔 User ID: {first_uid}")
    lines.append("⚡ Secured instantly by joining first")
    lines.append(f"{LINE}")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname}  | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1
    lines.append("")
    lines.append("✅ This giveaway was completed using a")
    lines.append("100% fair & transparent random system.")
    lines.append("🔐 User ID based selection only.")
    lines.append("")
    lines.append("🎁 PRIZE")
    lines.append(f"🏆 {prize}")
    lines.append("")
    lines.append("⏰ Important:")
    lines.append("🎁 Winners must claim their prize within 24 hours.")
    lines.append("❌ Unclaimed prizes will automatically expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: ⚡ {HOST_NAME}")
    lines.append("👇 Tap the button below to claim your prize 👇")
    return "\n".join(lines)


# =========================================================
# JOB CONTROL
# =========================================================
def stop_job(job):
    if job is not None:
        try:
            job.schedule_removal()
        except Exception:
            pass
    return None


def stop_live():
    global live_job
    live_job = stop_job(live_job)


def stop_closed_spin():
    global closed_spin_job
    closed_spin_job = stop_job(closed_spin_job)


def stop_draw():
    global draw_job, draw_finalize_job
    draw_job = stop_job(draw_job)
    draw_finalize_job = stop_job(draw_finalize_job)


def stop_reset_jobs():
    global reset_job, reset_finalize_job
    reset_job = stop_job(reset_job)
    reset_finalize_job = stop_job(reset_finalize_job)


# =========================================================
# LIVE COUNTDOWN UPDATE (CHANNEL LIVE POST)
# =========================================================
async def live_tick(context: ContextTypes.DEFAULT_TYPE):
    with lock:
        if not data.get("active"):
            stop_live()
            return

        start_ts = data.get("start_ts")
        if not start_ts:
            data["start_ts"] = now_ts()
            save_data()
            start_ts = data["start_ts"]

        duration = int(data.get("duration_seconds", 1) or 1)
        elapsed = int(now_ts() - float(start_ts))
        remaining = duration - elapsed

        live_mid = data.get("live_message_id")

    if remaining <= 0:
        # auto close
        await close_giveaway_and_post_end(context, reason="auto_end")
        stop_live()
        return

    if not live_mid:
        return

    try:
        await context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=live_mid,
            text=build_live_text(remaining),
            reply_markup=join_button_markup(),
        )
    except Exception:
        pass


def start_live(app: Application):
    global live_job
    stop_live()
    live_job = app.job_queue.run_repeating(live_tick, interval=5, first=0, name="live_job")


# =========================================================
# CLOSED SPIN (EDIT ENDED POST WITH SPINNER ONLY)
# =========================================================
async def closed_spin_tick(context: ContextTypes.DEFAULT_TYPE):
    # Just keep editing message (spinner feels "live")
    with lock:
        mid = data.get("closed_message_id")
        if not mid:
            stop_closed_spin()
            return
        if data.get("winners_message_id"):
            stop_closed_spin()
            return

    # small spin change by rotating icon in the text
    tick = context.job.data.get("tick", 0) + 1
    context.job.data["tick"] = tick
    spin = SPIN[(tick - 1) % len(SPIN)]

    # inject spin line (replace the "🔄 Please..." line)
    base = build_giveaway_ended_text().splitlines()
    out = []
    for line in base:
        if line.strip().startswith("🔄 Please"):
            out.append(f"{spin} Please stay tuned for the official announcement.")
        else:
            out.append(line)
    text = "\n".join(out)

    try:
        await context.bot.edit_message_text(chat_id=CHANNEL_ID, message_id=mid, text=text)
    except Exception:
        pass


def start_closed_spin(app: Application):
    global closed_spin_job
    stop_closed_spin()
    closed_spin_job = app.job_queue.run_repeating(
        closed_spin_tick, interval=1, first=0, data={"tick": 0}, name="closed_spin"
    )


# =========================================================
# GIVEAWAY END HANDLER (AUTO/MANUAL)
# =========================================================
async def close_giveaway_and_post_end(context: ContextTypes.DEFAULT_TYPE, reason: str):
    """
    - Delete live giveaway post
    - Post ended message
    - Start spinner animation
    - If auto_winner ON -> start auto draw (progress) then post winners
      and remove ended post after winners posted
    """
    with lock:
        live_mid = data.get("live_message_id")
        data["active"] = False
        data["closed"] = True
        save_data()

    if live_mid:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
        except Exception:
            pass

    # Remove old winners post if exists (new giveaway should overwrite, but keep clean)
    with lock:
        old_winners_mid = data.get("winners_message_id")
    if old_winners_mid:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=old_winners_mid)
        except Exception:
            pass
        with lock:
            data["winners_message_id"] = None
            save_data()

    # post ended
    try:
        m = await context.bot.send_message(chat_id=CHANNEL_ID, text=build_giveaway_ended_text())
        with lock:
            data["closed_message_id"] = m.message_id
            data["live_message_id"] = None
            save_data()
    except Exception:
        return

    # start spin
    try:
        start_closed_spin(context.application)
    except Exception:
        pass

    # notify admin
    auto_on = bool(data.get("auto_winner"))
    try:
        if auto_on:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="⏰ Giveaway Closed!\nAuto winner is ON ✅\n\nWinners will be selected automatically.",
            )
        else:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text="⏰ Giveaway Closed!\nAuto winner is OFF ❌\n\nNow use /draw to select winners.",
            )
    except Exception:
        pass

    if auto_on:
        # start auto draw now
        await start_draw_progress(context, ADMIN_ID, auto_mode=True)


# =========================================================
# DRAW PROGRESS (ADMIN MSG) + FINALIZE
# =========================================================
async def start_draw_progress(context: ContextTypes.DEFAULT_TYPE, admin_chat_id: int, auto_mode: bool):
    """
    Shows % + progress bar every 5 seconds
    - auto_mode=True: when finished, post winners directly to channel
    - auto_mode=False: send preview to admin with approve/reject
    """
    stop_draw()

    # if no participants
    with lock:
        if not (data.get("participants") or {}):
            try:
                await context.bot.send_message(chat_id=admin_chat_id, text="No participants to draw winners from.")
            except Exception:
                pass
            return

    seconds_total = AUTO_DRAW_SECONDS if auto_mode else MANUAL_DRAW_SECONDS
    msg = await context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, SPIN[0]),
    )

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
        "seconds_total": seconds_total,
        "auto_mode": auto_mode,
    }

    async def draw_tick(job_ctx: ContextTypes.DEFAULT_TYPE):
        jd = job_ctx.job.data
        jd["tick"] += 1
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(jd["seconds_total"])) * 100)))
        spin = SPIN[(jd["tick"] - 1) % len(SPIN)]
        try:
            await job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, spin),
            )
        except Exception:
            pass

    async def draw_finalize(job_ctx: ContextTypes.DEFAULT_TYPE):
        # compute winners
        with lock:
            participants = data.get("participants", {}) or {}
            winner_count = int(data.get("winner_count", 1) or 1)
            winner_count = max(1, winner_count)

            first_uid = data.get("first_winner_id")
            if not first_uid and participants:
                first_uid = next(iter(participants.keys()))
                info = participants.get(first_uid, {}) or {}
                data["first_winner_id"] = first_uid
                data["first_winner_username"] = info.get("username", "")
                data["first_winner_name"] = info.get("name", "")
                save_data()

            if not first_uid:
                # fallback
                first_uid = next(iter(participants.keys()))

            first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

            pool = [uid for uid in participants.keys() if uid != first_uid]
            remaining_needed = max(0, winner_count - 1)
            remaining_needed = min(remaining_needed, len(pool))
            selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

            winners_map = {first_uid: {"username": first_uname}}
            random_list = []
            for uid in selected:
                info = participants.get(uid, {}) or {}
                winners_map[uid] = {"username": info.get("username", "")}
                random_list.append((uid, info.get("username", "")))

            data["winners"] = winners_map
            pending_text = build_winners_post_text(first_uid, first_uname, random_list)
            data["pending_winners_text"] = pending_text
            save_data()

        # stop spinner on ended msg stays until winners posted, then we remove ended msg
        stop_draw()

        if jd.get("auto_mode"):
            # direct post to channel (no approve)
            await post_winners_to_channel(job_ctx, pending_text)
            try:
                await job_ctx.bot.edit_message_text(
                    chat_id=jd["admin_chat_id"],
                    message_id=jd["admin_msg_id"],
                    text="✅ Auto winners selected & posted to channel!",
                )
            except Exception:
                pass
        else:
            # send preview to admin with approve/reject
            try:
                await job_ctx.bot.edit_message_text(
                    chat_id=jd["admin_chat_id"],
                    message_id=jd["admin_msg_id"],
                    text=pending_text,
                    reply_markup=winners_approve_markup(),
                )
            except Exception:
                try:
                    await job_ctx.bot.send_message(
                        chat_id=jd["admin_chat_id"],
                        text=pending_text,
                        reply_markup=winners_approve_markup(),
                    )
                except Exception:
                    pass

    # schedule jobs
    global draw_job, draw_finalize_job
    draw_job = context.application.job_queue.run_repeating(draw_tick, interval=DRAW_TICK_SECONDS, first=0, data=ctx, name="draw_job")
    draw_finalize_job = context.application.job_queue.run_once(draw_finalize, when=seconds_total, data=ctx, name="draw_finalize_job")


async def post_winners_to_channel(context: ContextTypes.DEFAULT_TYPE, text: str):
    # remove ended message
    with lock:
        ended_mid = data.get("closed_message_id")
        old_winners_mid = data.get("winners_message_id")

    # delete "ended" post after winner selection done (your request)
    if ended_mid:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=ended_mid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

    # remove previous winners post if exists (replace)
    if old_winners_mid:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=old_winners_mid)
        except Exception:
            pass
        with lock:
            data["winners_message_id"] = None
            save_data()

    # post winners
    try:
        m = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=text,
            reply_markup=claim_button_markup(),
        )
        with lock:
            data["winners_message_id"] = m.message_id
            data["pending_winners_text"] = ""
            # claim window 24h
            ts = now_ts()
            data["claim_start_ts"] = ts
            data["claim_expires_ts"] = ts + 24 * 3600
            save_data()
    except Exception:
        return

    # stop closed spinner
    stop_closed_spin()

    # save history automatically (your request)
    await save_winner_history()


async def save_winner_history():
    """
    When winners are posted to channel, save history entries.
    """
    with lock:
        winners = data.get("winners", {}) or {}
        title = (data.get("title") or "").strip()
        prize = (data.get("prize") or "").strip()
        ts = now_ts()

        # build unique per post (same ts)
        hist = data.get("winner_history", []) or []

        for uid, info in winners.items():
            hist.append({
                "ts": ts,
                "title": title,
                "prize": prize,
                "uid": str(uid),
                "username": (info or {}).get("username", "") or "",
            })

        # keep last 5000 records (safe)
        if len(hist) > 5000:
            hist = hist[-5000:]

        data["winner_history"] = hist
        save_data()


# =========================================================
# RESET (40s progress, 5 sec interval, % + bar only)
# =========================================================
async def start_reset_progress(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    stop_reset_jobs()

    ctx = {
        "chat_id": chat_id,
        "message_id": message_id,
        "start_ts": now_ts(),
        "tick": 0,
        "seconds_total": RESET_SECONDS,
    }

    async def reset_tick(job_ctx: ContextTypes.DEFAULT_TYPE):
        jd = job_ctx.job.data
        jd["tick"] += 1
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(jd["seconds_total"])) * 100)))
        bar = build_progress(percent)
        try:
            await job_ctx.bot.edit_message_text(
                chat_id=jd["chat_id"],
                message_id=jd["message_id"],
                text=(
                    f"{LINE2}\n"
                    "♻️ RESET IN PROGRESS\n"
                    f"{LINE2}\n\n"
                    f"📊 {bar} {percent}%\n\n"
                    "⚠️ Reset will remove EVERYTHING.\n"
                    "Please wait..."
                ),
            )
        except Exception:
            pass

    async def reset_finalize(job_ctx: ContextTypes.DEFAULT_TYPE):
        # perform full reset (remove all posts if possible)
        stop_reset_jobs()
        stop_live()
        stop_draw()
        stop_closed_spin()

        with lock:
            live_mid = data.get("live_message_id")
            ended_mid = data.get("closed_message_id")
            winners_mid = data.get("winners_message_id")

        for mid in [live_mid, ended_mid, winners_mid]:
            if mid:
                try:
                    await job_ctx.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        with lock:
            data.clear()
            data.update(fresh_default_data())
            save_data()

        try:
            await job_ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"{LINE2}\n"
                    "✅ RESET COMPLETED SUCCESSFULLY!\n"
                    f"{LINE2}\n\n"
                    "Start again with:\n"
                    "/newgiveaway"
                ),
            )
        except Exception:
            pass

    global reset_job, reset_finalize_job
    reset_job = context.application.job_queue.run_repeating(reset_tick, interval=RESET_TICK_SECONDS, first=0, data=ctx, name="reset_job")
    reset_finalize_job = context.application.job_queue.run_once(reset_finalize, when=RESET_SECONDS, data=ctx, name="reset_finalize_job")


# =========================================================
# COMMANDS
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        await update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open Admin Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        await update.message.reply_text(
            f"{LINE2}\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            f"{LINE2}\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL – POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n\n"
        "⚡ AUTO SYSTEM\n"
        "/autowinnerpost\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "📜 HISTORY\n"
        "/winnerlist\n\n"
        "♻️ RESET\n"
        "/reset"
    )


async def cmd_autowinnerpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    status = "✅ ON" if data.get("auto_winner") else "❌ OFF"
    await update.message.reply_text(
        f"{LINE2}\n"
        "⚡ AUTO WINNER POST SYSTEM\n"
        f"{LINE2}\n\n"
        f"Current Status: {status}\n\n"
        "Choose ON or OFF:",
        reply_markup=autowinner_markup()
    )


async def cmd_addverifylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    await update.message.reply_text(
        f"{LINE2}\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        f"{LINE2}\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak\n"
    )


async def cmd_removeverifylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    if not is_admin(update):
        return

    targets = data.get("verify_targets", []) or []
    if not targets:
        await update.message.reply_text("No verify targets are set.")
        return

    lines = [
        LINE2,
        "🗑 REMOVE VERIFY TARGET",
        LINE2,
        "",
        "Current Verify Targets:",
        "",
    ]
    for i, t in enumerate(targets, start=1):
        lines.append(f"{i}) {t.get('display','')}")
    lines += ["", "Send a number to remove that target.", "11) Remove ALL verify targets"]
    admin_state = "remove_verify_pick"
    await update.message.reply_text("\n".join(lines))


async def cmd_newgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    if not is_admin(update):
        return

    stop_live()
    stop_draw()
    stop_closed_spin()
    stop_reset_jobs()

    with lock:
        # Keep verify + permanent bans across new giveaway
        keep_verify = data.get("verify_targets", [])
        keep_perma = data.get("permanent_block", {})
        keep_history = data.get("winner_history", [])

        data.clear()
        data.update(fresh_default_data())
        data["verify_targets"] = keep_verify
        data["permanent_block"] = keep_perma
        data["winner_history"] = keep_history
        save_data()

    admin_state = "title"
    await update.message.reply_text(
        f"{LINE2}\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        f"{LINE2}\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )


async def cmd_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    parts = data.get("participants", {}) or {}
    if not parts:
        await update.message.reply_text("👥 Participants List is empty.")
        return

    lines = [
        LINE2,
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        LINE2,
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
    if not is_admin(update):
        return
    if not data.get("active"):
        await update.message.reply_text("No active giveaway is running right now.")
        return

    await update.message.reply_text(
        f"{LINE2}\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        f"{LINE2}\n\n"
        "Are you sure you want to end this giveaway now?\n\n"
        "✅ Confirm End → Giveaway will close\n"
        "❌ Cancel → Giveaway will continue",
        reply_markup=end_confirm_markup()
    )


async def cmd_draw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not data.get("closed"):
        await update.message.reply_text("Giveaway is not closed yet or no giveaway running.")
        return
    if not (data.get("participants") or {}):
        await update.message.reply_text("No participants to draw winners from.")
        return

    await start_draw_progress(context, update.effective_chat.id, auto_mode=False)


async def cmd_blockpermanent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    await update.message.reply_text(
        f"{LINE2}\n"
        "🔒 PERMANENT BLOCK\n"
        f"{LINE2}\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "unban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
        ]]
    )
    await update.message.reply_text("Choose Unban Type:", reply_markup=kb)


async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}

    lines = []
    lines.append(LINE2)
    lines.append("📌 BAN LISTS")
    lines.append(LINE2)
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
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


async def cmd_winnerlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    hist = data.get("winner_history", []) or []
    if not hist:
        await update.message.reply_text("No winner history found yet.")
        return

    # show last 50
    hist = hist[-50:]
    lines = []
    lines.append(LINE2)
    lines.append("🏆 WINNER HISTORY (LAST 50)")
    lines.append(LINE2)
    lines.append("")
    for i, h in enumerate(reversed(hist), start=1):
        ts = dt_text(h.get("ts", 0))
        title = (h.get("title") or "").strip() or "Giveaway"
        prize = (h.get("prize") or "").strip() or "Prize"
        uid = h.get("uid", "")
        uname = h.get("username", "") or "(no username)"
        lines.append(f"{i}) 👤 {uname} | 🆔 {uid}")
        lines.append(f"   🎁 {prize}")
        lines.append(f"   🗓 {ts}")
        lines.append(f"   ⚡ {title}")
        lines.append("")

    await update.message.reply_text("\n".join(lines))


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    m = await update.message.reply_text(
        f"{LINE2}\n"
        "⚠️ RESET CONFIRMATION\n"
        f"{LINE2}\n\n"
        "This will remove EVERYTHING.\n"
        "Are you sure?",
        reply_markup=reset_confirm_markup(),
    )
    context.user_data["reset_msg_id"] = m.message_id


# =========================================================
# ADMIN TEXT FLOW
# =========================================================
async def admin_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    if not is_admin(update):
        return
    if admin_state is None:
        return

    msg = (update.message.text or "").strip()
    if not msg:
        return

    # ADD VERIFY
    if admin_state == "add_verify":
        ref = normalize_verify_ref(msg)
        if not ref:
            await update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= 100:
                await update.message.reply_text("Max verify targets reached (100). Remove some first.")
                return
            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        await update.message.reply_text(
            f"{LINE2}\n"
            "✅ VERIFY TARGET ADDED!\n"
            f"{LINE2}\n\n"
            f"Added: {ref}\n"
            f"Total: {len(data.get('verify_targets', []) or [])}\n\n"
            "Add more or Done?",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # REMOVE VERIFY PICK
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            await update.message.reply_text("Send a valid number (1,2,3... or 11).")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if not targets:
                admin_state = None
                await update.message.reply_text("No verify targets remain.")
                return

            if n == 11:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                await update.message.reply_text("✅ All verify targets removed successfully!")
                return

            if n < 1 or n > len(targets):
                await update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        await update.message.reply_text(
            f"{LINE2}\n"
            "✅ VERIFY TARGET REMOVED\n"
            f"{LINE2}\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}"
        )
        return

    # GIVEAWAY SETUP
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        await update.message.reply_text("✅ Title saved!\n\nNow send Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        await update.message.reply_text(
            "✅ Prize saved!\n\nNow send Total Winner Count (1 - 1000000):\n\n"
            "Note: 10 winners মানে 10 জন পাবে (প্রতিজন ১টা করে)."
        )
        return

    if admin_state == "winners":
        if not msg.isdigit():
            await update.message.reply_text("Please send a valid number for winner count.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            data["winner_count"] = count
            save_data()
        admin_state = "duration"
        await update.message.reply_text(
            f"✅ Winner count saved! Total Winners: {count}\n\n"
            "⏱ Send Giveaway Duration\n"
            "Example:\n"
            "30 Second\n"
            "30 Minute\n"
            "11 Hour"
        )
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            await update.message.reply_text("Invalid duration. Example: 30 Second / 30 Minute / 11 Hour")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()

        admin_state = "old_winner_mode"
        await update.message.reply_text(
            f"{LINE2}\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            f"{LINE2}\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n"
            "• Old winners can also win\n\n"
            "Reply: 1 or 2"
        )
        return

    if admin_state == "old_winner_mode":
        if msg not in ("1", "2"):
            await update.message.reply_text("Reply with 1 or 2 only.")
            return

        if msg == "2":
            with lock:
                data["old_winner_mode"] = "skip"
                data["old_winners"] = {}
                save_data()
            admin_state = "rules"
            await update.message.reply_text("✅ Old Winner Mode: SKIP\n\nNow send Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        await update.message.reply_text(
            f"{LINE2}\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            f"{LINE2}\n\n"
            "Send old winners list (one per line):\n"
            "@username | user_id\n"
            "OR\n"
            "user_id"
        )
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            save_data()

        admin_state = "rules"
        await update.message.reply_text(
            f"{LINE2}\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            f"{LINE2}\n\n"
            f"Added: {len(data['old_winners']) - before}\n\n"
            "Now send Rules (multi-line):"
        )
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        await update.message.reply_text("✅ Rules saved!\nShowing preview…")
        await update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMANENT BLOCK
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[uid] = {"username": uname}
            data["permanent_block"] = perma
            save_data()
        admin_state = None
        await update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # UNBAN INPUT HANDLERS
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            perma = data.get("permanent_block", {}) or {}
            if uid in perma:
                del perma[uid]
                data["permanent_block"] = perma
                save_data()
                await update.message.reply_text("✅ Unbanned from Permanent Block successfully!")
            else:
                await update.message.reply_text("This user id is not in Permanent Block list.")
        admin_state = None
        return

    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            await update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            ow = data.get("old_winners", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winners"] = ow
                save_data()
                await update.message.reply_text("✅ Unbanned from Old Winner Block successfully!")
            else:
                await update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state = None
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # AUTO WINNER ON/OFF
    if qd in ("autowinner_on", "autowinner_off"):
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        with lock:
            data["auto_winner"] = (qd == "autowinner_on")
            save_data()
        await query.answer("Saved ✅", show_alert=False)
        status = "✅ ON" if data.get("auto_winner") else "❌ OFF"
        try:
            await query.edit_message_text(
                f"{LINE2}\n"
                "⚡ AUTO WINNER POST SYSTEM\n"
                f"{LINE2}\n\n"
                f"Current Status: {status}\n\n"
                "Choose ON or OFF:",
                reply_markup=autowinner_markup()
            )
        except Exception:
            pass
        return

    # Verify buttons
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        admin_state = "add_verify"
        try:
            await query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        admin_state = None
        try:
            await query.edit_message_text(
                f"{LINE2}\n"
                "✅ VERIFY SETUP COMPLETED\n"
                f"{LINE2}\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "All users must join ALL targets to join giveaway."
            )
        except Exception:
            pass
        return

    # Preview actions
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return

        if qd == "preview_approve":
            await query.answer()

            # post live message to channel
            try:
                duration = int(data.get("duration_seconds", 0) or 1)
                m = await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_ts"] = now_ts()
                    data["closed_message_id"] = None
                    data["winners_message_id"] = None

                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""

                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""

                    data["claim_start_ts"] = None
                    data["claim_expires_ts"] = None

                    save_data()

                stop_closed_spin()
                start_live(context.application)

                await query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                await query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
            return

        if qd == "preview_reject":
            await query.answer()
            try:
                await query.edit_message_text("❌ Giveaway rejected and cleared.")
            except Exception:
                pass
            return

        if qd == "preview_edit":
            await query.answer()
            try:
                await query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            except Exception:
                pass
            return

    # End giveaway confirm/cancel
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        await close_giveaway_and_post_end(context, reason="manual_end")
        try:
            await query.edit_message_text("✅ Giveaway Closed Successfully!")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # RESET confirm/reject
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        # start reset progress (40s)
        try:
            await start_reset_progress(context, query.message.chat_id, query.message.message_id)
        except Exception:
            pass
        return

    if qd == "reset_reject":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text("❌ Reset rejected. Nothing changed.")
        except Exception:
            pass
        return

    # Unban choose
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        admin_state = "unban_permanent_input"
        try:
            await query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        admin_state = "unban_oldwinner_input"
        try:
            await query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            await query.answer("This giveaway is not active right now.", show_alert=True)
            return

        # verify required
        ok = await verify_user_join(context.bot, int(uid))
        if not ok:
            await query.answer(popup_verify_required(), show_alert=True)
            return

        # permanent block
        if uid in (data.get("permanent_block", {}) or {}):
            # Send DM so they can copy admin contact
            try:
                await context.bot.send_message(chat_id=int(uid), text=dm_permanent_blocked())
            except Exception:
                pass
            await query.answer("⛔ Permanently blocked. Check your inbox.", show_alert=True)
            return

        # old winner block
        if data.get("old_winner_mode") == "block" and uid in (data.get("old_winners", {}) or {}):
            await query.answer(popup_old_winner_blocked(), show_alert=True)
            return

        # already joined?
        if uid in (data.get("participants", {}) or {}):
            await query.answer(popup_already_joined(), show_alert=True)
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        is_first = False
        with lock:
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name
                is_first = True

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # update live post quickly
        try:
            with lock:
                live_mid = data.get("live_message_id")
                start_ts = data.get("start_ts")
                duration = int(data.get("duration_seconds", 1) or 1)
            if live_mid and start_ts:
                elapsed = int(now_ts() - float(start_ts))
                remaining = max(0, duration - elapsed)
                await context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        if is_first:
            await query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
        else:
            await query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
        return

    # Winners Approve/Reject
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try:
                await query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        await post_winners_to_channel(context, text)

        try:
            await query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
        except Exception:
            pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            await query.answer("Admin only.", show_alert=True)
            return
        await query.answer()
        with lock:
            data["pending_winners_text"] = ""
            save_data()
        try:
            await query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}
        if uid not in winners:
            await query.answer(popup_not_winner(), show_alert=True)
            return

        # expiry check
        exp = data.get("claim_expires_ts")
        if exp:
            try:
                if now_ts() > float(exp):
                    await query.answer("⏳ PRIZE EXPIRED\nYour 24-hour claim time has ended.", show_alert=True)
                    return
            except Exception:
                pass

        uname = winners.get(uid, {}).get("username", "") or user_tag(query.from_user.username or "") or "@username"

        # Send DM so user can copy ADMIN contact easily
        try:
            await context.bot.send_message(chat_id=int(uid), text=dm_claim_winner(uname, uid))
        except Exception:
            pass

        await query.answer("✅ Winner confirmed! Check your inbox for claim details.", show_alert=True)
        return

    # default
    try:
        await query.answer()
    except Exception:
        pass


# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")
    if ADMIN_ID == 0:
        raise SystemExit("ADMIN_ID missing in .env")
    if CHANNEL_ID == 0:
        raise SystemExit("CHANNEL_ID missing in .env")

    app = Application.builder().token(BOT_TOKEN).build()

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
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))

    app.add_handler(CommandHandler("winnerlist", cmd_winnerlist))
    app.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_handler))
    app.add_handler(CallbackQueryHandler(cb_handler))

    # resume after restart
    if data.get("active"):
        start_live(app)

    if data.get("closed") and data.get("closed_message_id") and not data.get("winners_message_id"):
        start_closed_spin(app)

    print("Bot running (PTB v20+ / GSM compatible) ...")
    app.run_polling()


if __name__ == "__main__":
    main()
