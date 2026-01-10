import os
import json
import random
import threading
import traceback
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Updater,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    Filters,
    CallbackContext,
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

# =========================================================
# THREAD SAFE STORAGE
# =========================================================
lock = threading.RLock()

# =========================================================
# GLOBAL STATE
# =========================================================
data = {}
admin_state = None

countdown_job = None
draw_job = None
draw_finalize_job = None

closed_anim_job = None
claim_expire_job = None

auto_channel_job = None
auto_channel_finalize_job = None

# =========================================================
# UTILS
# =========================================================
def now_ts() -> float:
    return datetime.utcnow().timestamp()

def safe_admin_log(bot, text: str):
    try:
        bot.send_message(chat_id=ADMIN_ID, text=(text or "")[:3500])
    except Exception:
        pass

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

# =========================================================
# DATA MODEL (supports multi winners posts)
# =========================================================
def fresh_default_data():
    return {
        # giveaway main
        "active": False,
        "closed": False,
        "title": "",
        "prize": "",
        "winner_count": 0,
        "duration_seconds": 0,
        "rules": "",
        "start_time": None,

        # channel messages
        "live_message_id": None,
        "closed_message_id": None,
        "winners_message_id": None,

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # setup old winner mode (during giveaway setup)
        "old_winner_mode": "skip",   # "block" or "skip"
        "old_winners": {},           # uid -> {"username": "@x"} only used if old_winner_mode="block"

        # command based old winner block (works always when ON)
        "blockoldwinner_enabled": False,
        "blockoldwinner_list": {},   # uid -> {"username": "@x" or ""}

        # first join winner
        "first_winner_id": None,
        "first_winner_username": "",
        "first_winner_name": "",

        # winners for current giveaway
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # claim window (current giveaway)
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # auto winner post system
        "auto_winner_post": False,

        # delivery tracking (GLOBAL across posts)
        "prize_delivered": {},  # uid -> {"username":"@x","ts":...}
        "prize_delivered_count": 0,

        # history of winners posts (message_id based)
        # winners_history[str(message_id)] = {
        #   "title":..., "prize":..., "winners":{uid:{"username":...}},
        #   "claim_start_ts":..., "claim_expires_ts":...,
        #   "verify_targets":[...],
        #   "delivered":{uid:True}
        # }
        "winners_history": {},
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
            json.dump(data, f, indent=4, ensure_ascii=False)

data = load_data()

def participants_count() -> int:
    return len(data.get("participants", {}) or {})

# =========================================================
# TELEGRAM MARKUPS
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

def autowinnerpost_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 ON", callback_data="auto_on"),
        InlineKeyboardButton("🔴 OFF", callback_data="auto_off"),
    ]])

def blockoldwinner_markup():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🟢 ON", callback_data="bow_on"),
        InlineKeyboardButton("🔴 OFF", callback_data="bow_off"),
    ]])

# =========================================================
# POPUP TEXTS (Premium)
# =========================================================
def popup_verify_required() -> str:
    return (
        "🔐 Access Restricted\n"
        "You must join the required channels to proceed.\n"
        "After joining, tap JOIN once more."
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
        "✨ CONGRATULATIONS 🌟\n"
        "You joined FIRST and secured the 🥇 1st Winner Spot!\n"
        f"👑 {username} | {uid}\n"
        "Take a screenshot & Post in the group to confirm your win 👈"
    )

def popup_already_joined() -> str:
    return (
        "❌ ENTRY Unsuccessful\n"
        "You’ve already joined\n"
        "this giveaway 🫵\n\n"
        "Multiple entries aren’t allowed.\n"
        "Please wait for the final result ⏳"
    )

def popup_join_success(username: str, uid: str) -> str:
    return (
        "🌹 CONGRATULATIONS!\n"
        "You’ve successfully joined\n"
        "the giveaway ✅\n\n"
        "Your details:\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n\n"
        f"— {HOST_NAME}"
    )

def popup_permanent_blocked() -> str:
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )

def popup_claim_winner(title: str, prize: str, username: str, uid: str) -> str:
    # admin contact plain so user can copy easily
    return (
        "🌟Congratulations ✨\n"
        "You’ve won this giveaway.✅\n"
        f"🎯 Giveaway: {title}\n"
        f"🎁 Prize: {prize}\n\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📩 Please contact admin & claim your prize now:\n"
        f"👉 {ADMIN_CONTACT}"
    )

def popup_claim_not_winner() -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "❌ NOT A WINNER\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Sorry! Your User ID is not in the winners list.\n"
        "Please wait for the next giveaway 🤍"
    )

def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )

def popup_prize_delivered() -> str:
    return (
        "🌟 Congratulations!\n"
        "Your prize has already been successfully delivered to you ✅\n"
        f"If you face any issues, please contact our admin 📩 {ADMIN_CONTACT}"
    )

# =========================================================
# VERIFY CHECK
# =========================================================
def verify_user_join(bot, user_id: int, verify_targets: list) -> bool:
    targets = verify_targets or []
    if not targets:
        return True
    for t in targets:
        ref = (t or {}).get("ref", "")
        if not ref:
            return False
        try:
            member = bot.get_chat_member(chat_id=ref, user_id=user_id)
            status = getattr(member, "status", None)
            if status not in ("member", "administrator", "creator"):
                return False
        except Exception:
            return False
    return True

# =========================================================
# TEXT BUILDERS (Premium Layout)
# =========================================================
def format_rules() -> str:
    rules = (data.get("rules") or "").strip()
    if not rules:
        return (
            "✅ Must join official channel\n"
            "❌ One account per user\n"
            "🚫 No fake / duplicate accounts"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join(lines)

def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    percent = 0
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN ONLY)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{data.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "📜 RULES...\n"
        f"{format_rules()}\n\n"
        f"📢 HOSTED BY ⚡ {HOST_NAME}\n"
        f"🔗 OFFICIAL CHANNEL: {CHANNEL_USERNAME}\n\n"
        "👇 TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    bar = build_progress(percent)

    # ✅ title only what admin sets
    title = (data.get("title") or "").strip()

    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {title} ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL ✨\n"
        f"{data.get('prize','')}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Randomly\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{bar} {percent}%\n\n"
        "📜 RULES...\n"
        f"{format_rules()}\n\n"
        f"📢 HOSTED BY ⚡ {HOST_NAME}\n"
        f"🔗 OFFICIAL CHANNEL: {CHANNEL_USERNAME}\n\n"
        "👇 TAP THE BUTTON BELOW & JOIN NOW 👇"
    )

CLOSE_SPINNER = ["🔄", "🔃", "🔁", "🔂"]

def build_closed_post_text(spin: str) -> str:
    # ✅ full version (no short)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        f"{spin} Winner selection is currently in progress...\n"
        "Please wait for the official announcement.\n\n"
        "🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )

def build_winners_post_text(title: str, prize: str, first_uid: str, first_user: str, random_winners: list) -> str:
    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    if title:
        lines.append(f"⚡ {title} ⚡")
        lines.append("")
    if prize:
        lines.append("🎁 PRIZE:")
        lines.append(str(prize))
        lines.append("")

    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        lines.append(f"👑 {first_user}")
        lines.append(f"🆔 {first_uid}")
    else:
        lines.append("👑 User ID")
        lines.append(f"🆔 {first_uid}")
    lines.append("🎯 Secured instantly by joining first")
    lines.append("")

    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1

    lines.append("")
    lines.append("⏳ Claim Rule:")
    lines.append("Prizes must be claimed within 24 hours.")
    lines.append("After 24 hours, claim will expire.")
    lines.append("")
    lines.append(f"📢 Hosted By: {HOST_NAME}")
    lines.append("👇 Click the button below to claim your prize")

    return "\n".join(lines)

# =========================================================
# LIVE COUNTDOWN
# =========================================================
def stop_live_countdown():
    global countdown_job
    if countdown_job is not None:
        try:
            countdown_job.schedule_removal()
        except Exception:
            pass
    countdown_job = None

def start_live_countdown(job_queue):
    global countdown_job
    stop_live_countdown()
    countdown_job = job_queue.run_repeating(live_tick, interval=5, first=0, name="live_countdown")

def stop_closed_anim():
    global closed_anim_job
    if closed_anim_job is not None:
        try:
            closed_anim_job.schedule_removal()
        except Exception:
            pass
    closed_anim_job = None

def start_closed_anim(job_queue):
    global closed_anim_job
    stop_closed_anim()
    closed_anim_job = job_queue.run_repeating(
        closed_anim_tick,
        interval=2,   # smooth
        first=0,
        context={"tick": 0},
        name="closed_anim",
    )

def closed_anim_tick(context: CallbackContext):
    # only when auto_winner_post OFF
    if data.get("auto_winner_post"):
        stop_closed_anim()
        return
    if data.get("winners_message_id"):
        stop_closed_anim()
        return
    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_anim()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    spin = CLOSE_SPINNER[(tick - 1) % len(CLOSE_SPINNER)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(spin)
        )
    except Exception:
        pass

def live_tick(context: CallbackContext):
    try:
        with lock:
            if not data.get("active"):
                stop_live_countdown()
                return

            start_time = data.get("start_time")
            if start_time is None:
                data["start_time"] = now_ts()
                save_data()
                start_time = data["start_time"]

            start = datetime.utcfromtimestamp(start_time)
            duration = data.get("duration_seconds", 1) or 1
            elapsed = int((datetime.utcnow() - start).total_seconds())
            remaining = duration - elapsed

            live_mid = data.get("live_message_id")

            if remaining <= 0:
                # close giveaway
                data["active"] = False
                data["closed"] = True
                save_data()

                # delete live message
                if live_mid:
                    try:
                        context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                    except Exception:
                        pass

                # post closed message
                try:
                    m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text("🔄"))
                    data["closed_message_id"] = m.message_id
                    save_data()

                    # OFF হলে spinner animation চলবে
                    if not data.get("auto_winner_post"):
                        start_closed_anim(context.job_queue)
                except Exception:
                    pass

                # AUTO MODE -> channel এ auto selection + auto post
                if data.get("auto_winner_post"):
                    try:
                        auto_channel_winner_selection(context)
                    except Exception:
                        safe_admin_log(context.bot, "AUTO WINNER ERROR:\n" + traceback.format_exc())

                # notify admin
                try:
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⏰ Giveaway Closed Automatically!\n\n"
                            f"Giveaway: {data.get('title','')}\n"
                            f"Total Participants: {participants_count()}\n\n"
                            + ("✅ Auto Winner Post: ON (channel will auto post winners)"
                               if data.get("auto_winner_post")
                               else "Now use /draw to select winners.")
                        ),
                    )
                except Exception:
                    pass

                stop_live_countdown()
                return

        if not live_mid:
            return

        try:
            context.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=live_mid,
                text=build_live_text(remaining),
                reply_markup=join_button_markup(),
            )
        except Exception:
            pass

    except Exception:
        safe_admin_log(context.bot, "LIVE TICK ERROR:\n" + traceback.format_exc())

# =========================================================
# DRAW (ADMIN) - FIXED
# =========================================================
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 5
SPINNER = ["🔄", "🔃", "🔁", "🔂"]

def stop_draw_jobs():
    global draw_job, draw_finalize_job
    if draw_job is not None:
        try:
            draw_job.schedule_removal()
        except Exception:
            pass
    draw_job = None
    if draw_finalize_job is not None:
        try:
            draw_finalize_job.schedule_removal()
        except Exception:
            pass
    draw_finalize_job = None

def build_draw_progress_text(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        "📊 Progress\n"
        f"{bar} {percent}%\n\n"
        "✅ 100% Random & Fair\n"
        "🔐 User ID based selection only.\n\n"
        "⏳ Please wait while system finalizes the winners...\n"
        f"— {HOST_NAME} ⚡"
    )

def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, SPINNER[0]),
    )

    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        # 99% পর্যন্ত; finalize এ 100% + winners
        percent = int(round(min(99, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))

        spin = SPINNER[(tick - 1) % len(SPINNER)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, spin),
            )
        except Exception:
            pass

    draw_job = context.job_queue.run_repeating(
        draw_tick,
        interval=DRAW_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="draw_progress_job",
    )

    draw_finalize_job = context.job_queue.run_once(
        draw_finalize,
        when=DRAW_DURATION_SECONDS + 1,
        context=ctx,
        name="draw_finalize_job",
    )

def draw_finalize(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context or {}
    admin_chat_id = jd.get("admin_chat_id")
    admin_msg_id = jd.get("admin_msg_id")

    try:
        with lock:
            participants = data.get("participants", {}) or {}
            if not participants:
                if admin_chat_id and admin_msg_id:
                    context.bot.edit_message_text(
                        chat_id=admin_chat_id,
                        message_id=admin_msg_id,
                        text="No participants to draw winners from.",
                    )
                return

            winner_count = max(1, int(data.get("winner_count", 1)) or 1)

            first_uid = data.get("first_winner_id")
            if not first_uid:
                first_uid = next(iter(participants.keys()))
                info = participants.get(first_uid, {}) or {}
                data["first_winner_id"] = first_uid
                data["first_winner_username"] = info.get("username", "")
                data["first_winner_name"] = info.get("name", "")

            first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

            pool = [uid for uid in participants.keys() if uid != first_uid]
            remaining_needed = min(max(0, winner_count - 1), len(pool))
            selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

            winners_map = {first_uid: {"username": first_uname}}
            random_list = []
            for uid in selected:
                info = participants.get(uid, {}) or {}
                winners_map[uid] = {"username": info.get("username", "")}
                random_list.append((uid, info.get("username", "")))

            data["winners"] = winners_map

            title = data.get("title", "")
            prize = data.get("prize", "")

            pending_text = build_winners_post_text(title, prize, first_uid, first_uname, random_list)
            data["pending_winners_text"] = pending_text
            save_data()

        # edit or send winners to admin for approve
        try:
            context.bot.edit_message_text(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                text=pending_text,
                reply_markup=winners_approve_markup(),
            )
        except Exception:
            context.bot.send_message(
                chat_id=admin_chat_id,
                text=pending_text,
                reply_markup=winners_approve_markup(),
            )

    except Exception:
        safe_admin_log(context.bot, "❌ DRAW FINALIZE FAILED!\n\n" + traceback.format_exc())

# =========================================================
# AUTO WINNER POST (CHANNEL) - 3 MIN ANIM
# =========================================================
AUTO_POST_DURATION = 180
AUTO_POST_INTERVAL = 5
AUTO_SPINNER = ["🔄", "🔃", "🔁", "🔂"]

def stop_auto_channel_jobs():
    global auto_channel_job, auto_channel_finalize_job
    if auto_channel_job:
        try:
            auto_channel_job.schedule_removal()
        except Exception:
            pass
    auto_channel_job = None
    if auto_channel_finalize_job:
        try:
            auto_channel_finalize_job.schedule_removal()
        except Exception:
            pass
    auto_channel_finalize_job = None

def build_auto_channel_progress(percent: int, spin: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Winner selection is in progress...\n\n"
        "📊 Progress\n"
        f"{bar} {percent}%\n\n"
        "✅ 100% Random & Fair\n"
        "🔐 User ID based selection only.\n\n"
        "⏳ Please wait while our secure system finalizes the winners...\n"
        f"— {HOST_NAME} ⚡"
    )

def auto_channel_winner_selection(context: CallbackContext):
    global auto_channel_job, auto_channel_finalize_job
    stop_auto_channel_jobs()

    closed_mid = data.get("closed_message_id")
    if not closed_mid:
        return

    ctx = {"start_ts": now_ts(), "tick": 0, "closed_mid": closed_mid}

    def auto_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        jd["tick"] = int(jd.get("tick", 0)) + 1
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(99, (elapsed / float(AUTO_POST_DURATION)) * 100)))
        spin = AUTO_SPINNER[(jd["tick"] - 1) % len(AUTO_SPINNER)]
        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=jd["closed_mid"],
                text=build_auto_channel_progress(percent, spin),
            )
        except Exception:
            pass

    auto_channel_job = context.job_queue.run_repeating(
        auto_tick,
        interval=AUTO_POST_INTERVAL,
        first=0,
        context=ctx,
        name="auto_channel_progress",
    )

    auto_channel_finalize_job = context.job_queue.run_once(
        auto_channel_finalize,
        when=AUTO_POST_DURATION + 1,
        context=ctx,
        name="auto_channel_finalize",
    )

def auto_channel_finalize(context: CallbackContext):
    stop_auto_channel_jobs()

    try:
        with lock:
            participants = data.get("participants", {}) or {}
            if not participants:
                return

            winner_count = max(1, int(data.get("winner_count", 1)) or 1)

            first_uid = data.get("first_winner_id")
            if not first_uid:
                first_uid = next(iter(participants.keys()))
                info = participants.get(first_uid, {}) or {}
                data["first_winner_id"] = first_uid
                data["first_winner_username"] = info.get("username", "")
                data["first_winner_name"] = info.get("name", "")

            first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

            pool = [uid for uid in participants.keys() if uid != first_uid]
            remaining_needed = min(max(0, winner_count - 1), len(pool))
            selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

            winners_map = {first_uid: {"username": first_uname}}
            random_list = []
            for uid in selected:
                info = participants.get(uid, {}) or {}
                winners_map[uid] = {"username": info.get("username", "")}
                random_list.append((uid, info.get("username", "")))

            data["winners"] = winners_map

            title = data.get("title", "")
            prize = data.get("prize", "")
            winners_text = build_winners_post_text(title, prize, first_uid, first_uname, random_list)
            data["pending_winners_text"] = winners_text  # keep for admin
            save_data()

        # delete closed msg
        cmid = data.get("closed_message_id")
        if cmid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=cmid)
            except Exception:
                pass

        # post winners in channel (direct)
        m = context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=winners_text,
            reply_markup=claim_button_markup(),
        )

        with lock:
            data["winners_message_id"] = m.message_id
            data["closed_message_id"] = None

            ts = now_ts()
            data["claim_start_ts"] = ts
            data["claim_expires_ts"] = ts + 24 * 3600
            save_data()

            # snapshot this winners post in history
            snapshot_winners_post(m.message_id, title, prize, data["winners"], data["claim_start_ts"], data["claim_expires_ts"], data.get("verify_targets", []))

        schedule_claim_expire(context.job_queue)

    except Exception:
        safe_admin_log(context.bot, "AUTO FINALIZE ERROR:\n" + traceback.format_exc())

# =========================================================
# CLAIM EXPIRY
# =========================================================
def stop_claim_expire_job():
    global claim_expire_job
    if claim_expire_job is not None:
        try:
            claim_expire_job.schedule_removal()
        except Exception:
            pass
    claim_expire_job = None

def schedule_claim_expire(job_queue):
    global claim_expire_job
    stop_claim_expire_job()

    exp = data.get("claim_expires_ts")
    if not exp:
        return

    remain = float(exp) - now_ts()
    if remain <= 0:
        return

    claim_expire_job = job_queue.run_once(expire_claim_button_job, when=remain, name="claim_expire_job")

def expire_claim_button_job(context: CallbackContext):
    with lock:
        mid = data.get("winners_message_id")
        exp = data.get("claim_expires_ts")
        if not mid or not exp:
            return
    try:
        context.bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=mid,
            reply_markup=None
        )
    except Exception:
        pass

# =========================================================
# WINNERS HISTORY SNAPSHOT (MULTI CLAIM POSTS)
# =========================================================
def snapshot_winners_post(message_id: int, title: str, prize: str, winners_map: dict, claim_start: float, claim_exp: float, verify_targets: list):
    hist = data.get("winners_history", {}) or {}
    mid = str(message_id)
    hist[mid] = {
        "title": title or "",
        "prize": prize or "",
        "winners": winners_map or {},
        "claim_start_ts": claim_start,
        "claim_expires_ts": claim_exp,
        "verify_targets": verify_targets or [],
        "delivered": {},  # uid->True
    }
    data["winners_history"] = hist
    save_data()

def get_post_context_for_claim(message_id: int):
    hist = data.get("winners_history", {}) or {}
    return hist.get(str(message_id))

# =========================================================
# RESET
# =========================================================
def do_full_reset(context: CallbackContext, admin_chat_id: int, admin_msg_id: int):
    global data
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_anim()
    stop_claim_expire_job()
    stop_auto_channel_jobs()

    with lock:
        for mid_key in ["live_message_id", "closed_message_id", "winners_message_id"]:
            mid = data.get(mid_key)
            if mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                except Exception:
                    pass

        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        keep_delivery = data.get("prize_delivered", {})
        keep_delivery_count = data.get("prize_delivered_count", 0)
        keep_bow_enabled = data.get("blockoldwinner_enabled", False)
        keep_bow_list = data.get("blockoldwinner_list", {})
        keep_auto = data.get("auto_winner_post", False)

        data = fresh_default_data()
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["prize_delivered"] = keep_delivery
        data["prize_delivered_count"] = keep_delivery_count
        data["blockoldwinner_enabled"] = keep_bow_enabled
        data["blockoldwinner_list"] = keep_bow_list
        data["auto_winner_post"] = keep_auto
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text=(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ RESET COMPLETED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Start again with:\n"
                "/newgiveaway"
            ),
        )
    except Exception:
        pass

# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️👑 WELCOME BACK, ADMIN 👑🛡️\n\n"
            "⚙️ System Status: ONLINE ✅\n"
            "🚀 Giveaway Engine: READY\n\n"
            "🧭 Open Admin Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )

def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    auto_st = "ON ✅" if data.get("auto_winner_post") else "OFF ❌"
    bow_st = "ON ✅" if data.get("blockoldwinner_enabled") else "OFF ❌"
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL – POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n\n"
        "⚙️ AUTO SYSTEM\n"
        f"/autowinnerpost  (Currently: {auto_st})\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "⛔ OLD WINNER COMMAND BLOCK\n"
        f"/blockoldwinner  (Currently: {bow_st})\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🎁 PRIZE DELIVERY\n"
        "/prizedelivery\n\n"
        "♻️ RESET\n"
        "/reset"
    )

def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    status = "ON ✅" if data.get("auto_winner_post") else "OFF ❌"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ AUTO WINNER POST SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Current Status: {status}\n\n"
        "🟢 ON  → Giveaway time শেষ হলে bot automatically\n"
        "        winners select করবে + channel এ 3 minute\n"
        "        progress/spinner দেখাবে + তারপর winners post.\n\n"
        "🔴 OFF → Giveaway close হবে, তারপর /draw দিয়ে winners দিবে.\n\n"
        "Choose an option below:",
        reply_markup=autowinnerpost_markup()
    )

def cmd_blockoldwinner(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    status = "ON ✅" if data.get("blockoldwinner_enabled") else "OFF ❌"
    total = len(data.get("blockoldwinner_list", {}) or {})
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⛔ OLD WINNER COMMAND BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Status: {status}\n"
        f"Total Blocked Here: {total}\n\n"
        "🟢 ON  → এই list এ যাদের দিবে তারা ALWAYS join করতে পারবে না\n"
        "        (Setup mode SKIP হলেও).\n\n"
        "🔴 OFF → এই command list ignore হবে.\n\n"
        "Choose:",
        reply_markup=blockoldwinner_markup()
    )

def cmd_prizedelivery(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivery_input"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🎁 PRIZE DELIVERY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Example:\n"
        "@minexxproo | 8293728\n"
        "123456789"
    )

def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak\n\n"
        "After adding, users must join ALL verify targets to join giveaway."
    )

def cmd_removeverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    targets = data.get("verify_targets", []) or []
    if not targets:
        update.message.reply_text("No verify targets are set.")
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
    lines += [
        "",
        "Send a number to remove that target.",
        "11) Remove ALL verify targets",
    ]
    admin_state = "remove_verify_pick"
    update.message.reply_text("\n".join(lines))

def cmd_newgiveaway(update: Update, context: CallbackContext):
    global admin_state, data
    if not is_admin(update):
        return

    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_anim()
    stop_claim_expire_job()
    stop_auto_channel_jobs()

    with lock:
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", [])
        keep_delivery = data.get("prize_delivered", {})
        keep_delivery_count = data.get("prize_delivered_count", 0)
        keep_bow_enabled = data.get("blockoldwinner_enabled", False)
        keep_bow_list = data.get("blockoldwinner_list", {})
        keep_auto = data.get("auto_winner_post", False)

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["prize_delivered"] = keep_delivery
        data["prize_delivered_count"] = keep_delivery_count
        data["blockoldwinner_enabled"] = keep_bow_enabled
        data["blockoldwinner_list"] = keep_bow_list
        data["auto_winner_post"] = keep_auto
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1️⃣ — GIVEAWAY TITLE\n\n"
        "Send Giveaway Title:"
    )

def cmd_participants(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    parts = data.get("participants", {})
    if not parts:
        update.message.reply_text("👥 Participants List is empty.")
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
        lines.append("")
        i += 1

    update.message.reply_text("\n".join(lines))

def cmd_endgiveaway(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("active"):
        update.message.reply_text("No active giveaway is running right now.")
        return

    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure you want to end this giveaway now?\n\n"
        "✅ Confirm End → Giveaway will close\n"
        "❌ Cancel → Giveaway will continue",
        reply_markup=end_confirm_markup()
    )

def cmd_draw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    if not data.get("closed"):
        update.message.reply_text("Giveaway is not closed yet or no giveaway running.")
        return
    if not data.get("participants", {}):
        update.message.reply_text("No participants to draw winners from.")
        return

    start_draw_progress(context, update.effective_chat.id)

def cmd_blockpermanent(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "perma_block_list"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send list (one per line):\n"
        "User ID only OR username + id\n\n"
        "Examples:\n"
        "7297292\n"
        "@MinexxProo | 7297292"
    )

def cmd_unban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    admin_state = "unban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Unban Permanent Block", callback_data="unban_permanent"),
            InlineKeyboardButton("Unban Old Winner Block (Setup)", callback_data="unban_oldwinner"),
            InlineKeyboardButton("Unban Old Winner Block (Command)", callback_data="unban_oldwinner_cmd"),
        ]]
    )
    update.message.reply_text("Choose Unban Type:", reply_markup=kb)

def cmd_removeban(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return

    admin_state = "removeban_choose"
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Reset Permanent Ban List", callback_data="reset_permanent_ban"),
            InlineKeyboardButton("Reset Old Winner Ban (Setup)", callback_data="reset_oldwinner_ban"),
            InlineKeyboardButton("Reset Old Winner Ban (Command)", callback_data="reset_oldwinner_cmd_ban"),
        ]]
    )
    update.message.reply_text("Choose which ban list to reset:", reply_markup=kb)

def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}
    cmdw = data.get("blockoldwinner_list", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE (SETUP): {data.get('old_winner_mode','skip').upper()}")
    lines.append(f"COMMAND BLOCK ENABLED: {'ON' if data.get('blockoldwinner_enabled') else 'OFF'}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST (SETUP)")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {(uname+' | ') if uname else ''}User ID: {uid}")
            i += 1
    else:
        lines.append("No setup old winner blocked users.")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST (COMMAND)")
    lines.append(f"Total: {len(cmdw)}")
    if cmdw:
        i = 1
        for uid, info in cmdw.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {(uname+' | ') if uname else ''}User ID: {uid}")
            i += 1
    else:
        lines.append("No command blocked users.")
    lines.append("")

    lines.append("🔒 PERMANENT BLOCK LIST")
    lines.append(f"Total: {len(perma)}")
    if perma:
        i = 1
        for uid, info in perma.items():
            uname = (info or {}).get("username", "")
            lines.append(f"{i}) {(uname+' | ') if uname else ''}User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    update.message.reply_text("\n".join(lines))

def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    m = update.message.reply_text(
        "Confirm reset?",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Confirm Reset", callback_data="reset_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="reset_cancel"),
            ]]
        )
    )
    context.user_data["reset_msg_id"] = m.message_id

# =========================================================
# ADMIN TEXT FLOW
# =========================================================
def admin_text_handler(update: Update, context: CallbackContext):
    global admin_state, data
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
            update.message.reply_text("Invalid input.\nSend Chat ID like -100123... or @username.")
            return

        with lock:
            targets = data.get("verify_targets", []) or []
            if len(targets) >= 100:
                update.message.reply_text("Max verify targets reached (100). Remove some first.")
                return

            targets.append({"ref": ref, "display": ref})
            data["verify_targets"] = targets
            save_data()

        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED SUCCESSFULLY!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n\n"
            "What do you want to do next?",
            reply_markup=verify_add_more_done_markup()
        )
        return

    # REMOVE VERIFY PICK
    if admin_state == "remove_verify_pick":
        if not msg.isdigit():
            update.message.reply_text("Send a valid number (1,2,3... or 11).")
            return
        n = int(msg)
        with lock:
            targets = data.get("verify_targets", []) or []
            if not targets:
                admin_state = None
                update.message.reply_text("No verify targets remain.")
                return

            if n == 11:
                data["verify_targets"] = []
                save_data()
                admin_state = None
                update.message.reply_text("✅ All verify targets removed successfully!")
                return

            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}"
        )
        return

    # PRIZE DELIVERY INPUT
    if admin_state == "prize_delivery_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            delivered = data.get("prize_delivered", {}) or {}
            before = len(delivered)
            for uid, uname in entries:
                delivered[uid] = {"username": uname, "ts": now_ts()}
            data["prize_delivered"] = delivered
            data["prize_delivered_count"] = len(delivered)
            save_data()

            # also mark delivered in each winners_history (so claim checks)
            hist = data.get("winners_history", {}) or {}
            for mid, post in hist.items():
                post_del = post.get("delivered", {}) or {}
                for uid, _ in entries:
                    post_del[uid] = True
                post["delivered"] = post_del
            data["winners_history"] = hist
            save_data()

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ PRIZE DELIVERY UPDATED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"New Added: {len(data['prize_delivered']) - before}\n"
            f"Total Delivered Count: {data.get('prize_delivered_count',0)}"
        )
        return

    # GIVEAWAY SETUP FLOW
    if admin_state == "title":
        with lock:
            data["title"] = msg
            save_data()
        admin_state = "prize"
        update.message.reply_text("✅ Title saved!\n\nNow send Giveaway Prize (multi-line allowed):")
        return

    if admin_state == "prize":
        with lock:
            data["prize"] = msg
            save_data()
        admin_state = "winners"
        update.message.reply_text("✅ Prize saved!\n\nNow send Total Winner Count (1 - 1000000):")
        return

    if admin_state == "winners":
        if not msg.isdigit():
            update.message.reply_text("Please send a valid number for winner count.")
            return
        count = max(1, min(1000000, int(msg)))
        with lock:
            data["winner_count"] = count
            save_data()
        admin_state = "duration"
        update.message.reply_text(
            "✅ Winner count saved!\n\n"
            f"🏆 Total Winners: {count}\n\n"
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
            update.message.reply_text("Invalid duration. Example: 30 Second / 30 Minute / 11 Hour")
            return

        with lock:
            data["duration_seconds"] = seconds
            save_data()

        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "1️⃣ BLOCK OLD WINNERS\n"
            "• Old winners cannot join this giveaway\n\n"
            "2️⃣ SKIP OLD WINNERS\n"
            "• Everyone can join\n\n"
            "Reply with:\n"
            "1 → BLOCK\n"
            "2 → SKIP"
        )
        return

    if admin_state == "old_winner_mode":
        if msg not in ("1", "2"):
            update.message.reply_text("Reply with 1 or 2 only.")
            return

        if msg == "2":
            with lock:
                data["old_winner_mode"] = "skip"
                data["old_winners"] = {}
                save_data()

            admin_state = "rules"
            update.message.reply_text(
                "📌 Old Winner Mode set to: SKIP\n"
                "✅ Everyone can join.\n\n"
                "Now send Giveaway Rules (multi-line):"
            )
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "556677"
        )
        return

    if admin_state == "old_winner_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            ow = data.get("old_winners", {}) or {}
            before = len(ow)
            for uid, uname in entries:
                ow[uid] = {"username": uname}
            data["old_winners"] = ow
            save_data()

        admin_state = "rules"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Total Added: {len(data['old_winners']) - before}\n"
            "🔒 These users can NOT join this giveaway.\n\n"
            "Now send Giveaway Rules (multi-line):"
        )
        return

    if admin_state == "rules":
        with lock:
            data["rules"] = msg
            save_data()
        admin_state = None
        update.message.reply_text("✅ Rules saved!\nShowing preview…")
        update.message.reply_text(build_preview_text(), reply_markup=preview_markup())
        return

    # PERMANENT BLOCK
    if admin_state == "perma_block_list":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            perma = data.get("permanent_block", {}) or {}
            before = len(perma)
            for uid, uname in entries:
                perma[uid] = {"username": uname}
            data["permanent_block"] = perma
            save_data()

        admin_state = None
        update.message.reply_text(
            "✅ Permanent block saved!\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # COMMAND OLD WINNER LIST INPUT
    if admin_state == "bow_list_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return
        with lock:
            bl = data.get("blockoldwinner_list", {}) or {}
            before = len(bl)
            for uid, uname in entries:
                bl[uid] = {"username": uname}
            data["blockoldwinner_list"] = bl
            save_data()

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ COMMAND OLD WINNER LIST UPDATED!\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"New Added: {len(data['blockoldwinner_list']) - before}\n"
            f"Total In List: {len(data['blockoldwinner_list'])}"
        )
        return

    # UNBAN INPUT HANDLERS
    if admin_state == "unban_permanent_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            perma = data.get("permanent_block", {}) or {}
            if uid in perma:
                del perma[uid]
                data["permanent_block"] = perma
                save_data()
                update.message.reply_text("✅ Unbanned from Permanent Block successfully!")
            else:
                update.message.reply_text("This user id is not in Permanent Block list.")
        admin_state = None
        return

    if admin_state == "unban_oldwinner_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            ow = data.get("old_winners", {}) or {}
            if uid in ow:
                del ow[uid]
                data["old_winners"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block (Setup) successfully!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block (Setup) list.")
        admin_state = None
        return

    if admin_state == "unban_oldwinner_cmd_input":
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send User ID (or @name | id)")
            return
        uid, _ = entries[0]
        with lock:
            ow = data.get("blockoldwinner_list", {}) or {}
            if uid in ow:
                del ow[uid]
                data["blockoldwinner_list"] = ow
                save_data()
                update.message.reply_text("✅ Unbanned from Old Winner Block (Command) successfully!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block (Command) list.")
        admin_state = None
        return

# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # -------------------------
    # AUTO WINNER POST SETTINGS
    # -------------------------
    if qd in ("auto_on", "auto_off"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return

        with lock:
            data["auto_winner_post"] = (qd == "auto_on")
            save_data()

        try:
            query.answer("Saved ✅", show_alert=False)
        except Exception:
            pass

        st = "ON ✅" if data["auto_winner_post"] else "OFF ❌"
        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ SETTINGS UPDATED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Auto Winner Post is now: {st}"
            )
        except Exception:
            pass
        return

    # -------------------------
    # BLOCK OLD WINNER SETTINGS
    # -------------------------
    if qd in ("bow_on", "bow_off"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return

        with lock:
            data["blockoldwinner_enabled"] = (qd == "bow_on")
            save_data()

        try:
            query.answer("Saved ✅", show_alert=False)
        except Exception:
            pass

        st = "ON ✅" if data["blockoldwinner_enabled"] else "OFF ❌"
        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ SETTINGS UPDATED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Command Old Winner Block is now: {st}\n\n"
                "If ON, now send list with:\n"
                "@username | user_id\n"
                "or user_id only\n\n"
                "Send list now:"
            )
        except Exception:
            pass

        if data.get("blockoldwinner_enabled"):
            admin_state = "bow_list_input"
        else:
            admin_state = None
        return

    # -------------------------
    # Verify buttons
    # -------------------------
    if qd == "verify_add_more":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        admin_state = "add_verify"
        try:
            query.edit_message_text("Send another Chat ID or @username:")
        except Exception:
            pass
        return

    if qd == "verify_add_done":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        admin_state = None
        try:
            query.edit_message_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ VERIFY SETUP COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "All users must join ALL targets to join giveaway."
            )
        except Exception:
            pass
        return

    # -------------------------
    # Preview actions
    # -------------------------
    if qd.startswith("preview_"):
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        if qd == "preview_approve":
            try:
                query.answer()
            except Exception:
                pass

            try:
                duration = int(data.get("duration_seconds", 0)) or 1
                m = context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=build_live_text(duration),
                    reply_markup=join_button_markup(),
                )

                with lock:
                    data["live_message_id"] = m.message_id
                    data["active"] = True
                    data["closed"] = False
                    data["start_time"] = now_ts()
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

                stop_closed_anim()
                stop_auto_channel_jobs()
                start_live_countdown(context.job_queue)

                query.edit_message_text("✅ Giveaway approved and posted to channel!")
            except Exception as e:
                query.edit_message_text(f"Failed to post in channel. Make sure bot is admin.\nError: {e}")
            return

        if qd == "preview_reject":
            try:
                query.answer()
            except Exception:
                pass
            query.edit_message_text("❌ Giveaway rejected and cleared.")
            return

        if qd == "preview_edit":
            try:
                query.answer()
            except Exception:
                pass
            query.edit_message_text("✏️ Edit Mode\n\nStart again with /newgiveaway")
            return

    # -------------------------
    # End giveaway confirm/cancel
    # -------------------------
    if qd == "end_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return

        try:
            query.answer()
        except Exception:
            pass

        with lock:
            if not data.get("active"):
                try:
                    query.edit_message_text("No active giveaway is running right now.")
                except Exception:
                    pass
                return

            data["active"] = False
            data["closed"] = True
            save_data()

        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text("🔄"))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()

            if not data.get("auto_winner_post"):
                start_closed_anim(context.job_queue)
            else:
                auto_channel_winner_selection(context)

        except Exception:
            pass

        stop_live_countdown()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully!")
        except Exception:
            pass
        return

    if qd == "end_cancel":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("❌ Cancelled. Giveaway is still running.")
        except Exception:
            pass
        return

    # -------------------------
    # Reset confirm/cancel
    # -------------------------
    if qd == "reset_confirm":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        do_full_reset(context, query.message.chat_id, query.message.message_id)
        return

    if qd == "reset_cancel":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        try:
            query.edit_message_text("❌ Reset cancelled.")
        except Exception:
            pass
        return

    # -------------------------
    # Unban choose
    # -------------------------
    if qd == "unban_permanent":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return
        try: query.answer()
        except: pass
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return
        try: query.answer()
        except: pass
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block (Setup):")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner_cmd":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return
        try: query.answer()
        except: pass
        admin_state = "unban_oldwinner_cmd_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block (Command):")
        except Exception:
            pass
        return

    # -------------------------
    # removeban choose confirm
    # -------------------------
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban", "reset_oldwinner_cmd_ban"):
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return

        try:
            query.answer()
        except Exception:
            pass

        if qd == "reset_permanent_ban":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
            ]])
            try: query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=kb)
            except: pass
            return

        if qd == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm Reset Old Winner (Setup)", callback_data="confirm_reset_oldwinner"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
            ]])
            try: query.edit_message_text("Confirm reset Old Winner Ban List (Setup)?", reply_markup=kb)
            except: pass
            return

        if qd == "reset_oldwinner_cmd_ban":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm Reset Old Winner (Command)", callback_data="confirm_reset_oldwinner_cmd"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
            ]])
            try: query.edit_message_text("Confirm reset Old Winner Ban List (Command)?", reply_markup=kb)
            except: pass
            return

    if qd == "cancel_reset_ban":
        try: query.answer()
        except: pass
        admin_state = None
        try: query.edit_message_text("Cancelled.")
        except: pass
        return

    if qd == "confirm_reset_permanent":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return
        try: query.answer()
        except: pass
        with lock:
            data["permanent_block"] = {}
            save_data()
        try: query.edit_message_text("✅ Permanent Ban List has been reset.")
        except: pass
        return

    if qd == "confirm_reset_oldwinner":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return
        try: query.answer()
        except: pass
        with lock:
            data["old_winners"] = {}
            save_data()
        try: query.edit_message_text("✅ Old Winner Ban List (Setup) has been reset.")
        except: pass
        return

    if qd == "confirm_reset_oldwinner_cmd":
        if uid != str(ADMIN_ID):
            try: query.answer("Admin only.", show_alert=True)
            except: pass
            return
        try: query.answer()
        except: pass
        with lock:
            data["blockoldwinner_list"] = {}
            save_data()
        try: query.edit_message_text("✅ Old Winner Ban List (Command) has been reset.")
        except: pass
        return

    # -------------------------
    # Join giveaway
    # -------------------------
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # verify check for join
        if not verify_user_join(context.bot, int(uid), data.get("verify_targets", [])):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # command based old winner block always when enabled
        if data.get("blockoldwinner_enabled"):
            if uid in (data.get("blockoldwinner_list", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # setup mode old winner block
        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        with lock:
            first_uid = data.get("first_winner_id")

        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try:
                query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            if not data.get("first_winner_id"):
                data["first_winner_id"] = uid
                data["first_winner_username"] = uname
                data["first_winner_name"] = full_name

            data["participants"][uid] = {"username": uname, "name": full_name}
            save_data()

        # update live post quickly
        try:
            live_mid = data.get("live_message_id")
            start_ts = data.get("start_time")
            if live_mid and start_ts:
                start = datetime.utcfromtimestamp(start_ts)
                duration = data.get("duration_seconds", 1) or 1
                elapsed = int((datetime.utcnow() - start).total_seconds())
                remaining = duration - elapsed
                if remaining < 0:
                    remaining = 0
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        with lock:
            if data.get("first_winner_id") == uid:
                try:
                    query.answer(popup_first_winner(uname or "@username", uid), show_alert=True)
                except Exception:
                    pass
            else:
                try:
                    query.answer(popup_join_success(uname or "@Username", uid), show_alert=True)
                except Exception:
                    pass
        return

    # -------------------------
    # Winners Approve/Reject (ADMIN manual mode)
    # -------------------------
    if qd == "winners_approve":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass

        text = (data.get("pending_winners_text") or "").strip()
        if not text:
            try:
                query.edit_message_text("No pending winners preview found.")
            except Exception:
                pass
            return

        stop_closed_anim()

        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                reply_markup=claim_button_markup(),
            )
            with lock:
                data["winners_message_id"] = m.message_id
                data["closed_message_id"] = None

                ts = now_ts()
                data["claim_start_ts"] = ts
                data["claim_expires_ts"] = ts + 24 * 3600
                save_data()

                # snapshot in history (multi claim posts)
                snapshot_winners_post(
                    m.message_id,
                    data.get("title",""),
                    data.get("prize",""),
                    data.get("winners", {}),
                    data["claim_start_ts"],
                    data["claim_expires_ts"],
                    data.get("verify_targets", [])
                )

            schedule_claim_expire(context.job_queue)

            query.edit_message_text("✅ Approved! Winners list posted to channel (with Claim button).")
        except Exception as e:
            try:
                query.edit_message_text(f"Failed to post winners in channel: {e}")
            except Exception:
                pass
        return

    if qd == "winners_reject":
        if uid != str(ADMIN_ID):
            try:
                query.answer("Admin only.", show_alert=True)
            except Exception:
                pass
            return
        try:
            query.answer()
        except Exception:
            pass
        with lock:
            data["pending_winners_text"] = ""
            save_data()
        try:
            query.edit_message_text("❌ Rejected! Winners list will NOT be posted.")
        except Exception:
            pass
        return

    # -------------------------
    # Claim prize (MULTI POST SUPPORT + VERIFY + DELIVERY)
    # -------------------------
    if qd == "claim_prize":
        # which post button clicked
        msg_id = None
        try:
            msg_id = query.message.message_id
        except Exception:
            msg_id = None

        post_ctx = get_post_context_for_claim(msg_id) if msg_id else None

        # fallback to current if not in history
        if not post_ctx:
            post_ctx = {
                "title": data.get("title",""),
                "prize": data.get("prize",""),
                "winners": data.get("winners", {}) or {},
                "claim_expires_ts": data.get("claim_expires_ts"),
                "verify_targets": data.get("verify_targets", []) or [],
                "delivered": {},
            }

        # verify check for claim
        if not verify_user_join(context.bot, int(uid), post_ctx.get("verify_targets", [])):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        winners = post_ctx.get("winners", {}) or {}

        # not winner
        if uid not in winners:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        # expired?
        exp_ts = post_ctx.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        # delivered already?
        delivered_map = post_ctx.get("delivered", {}) or {}
        global_delivered = data.get("prize_delivered", {}) or {}
        if delivered_map.get(uid) or (uid in global_delivered):
            try:
                query.answer(popup_prize_delivered(), show_alert=True)
            except Exception:
                pass
            return

        uname = winners.get(uid, {}).get("username", "") or "@username"
        title = post_ctx.get("title", "") or ""
        prize = post_ctx.get("prize", "") or ""

        try:
            query.answer(popup_claim_winner(title, prize, uname, uid), show_alert=True)
        except Exception:
            pass
        return

    try:
        query.answer()
    except Exception:
        pass

# =========================================================
# MAIN
# =========================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # basic
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # auto & block old winner & delivery
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))
    dp.add_handler(CommandHandler("blockoldwinner", cmd_blockoldwinner))
    dp.add_handler(CommandHandler("prizedelivery", cmd_prizedelivery))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Resume systems after restart
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    # closed spinner only when auto off
    if data.get("closed") and data.get("closed_message_id") and not data.get("winners_message_id") and not data.get("auto_winner_post"):
        start_closed_anim(updater.job_queue)

    # claim expiry
    if data.get("winners_message_id") and data.get("claim_expires_ts"):
        remain = float(data["claim_expires_ts"]) - now_ts()
        if remain > 0:
            schedule_claim_expire(updater.job_queue)

    print("Bot is running (PTB 13.15, non-async) ...")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
