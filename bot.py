import os
import re
import json
import random
import threading
from datetime import datetime, timedelta

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

# Your timezone: Asia/Dhaka (+06:00)
BD_OFFSET = timedelta(hours=6)

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
closed_spinner_job = None

draw_job = None
draw_finalize_job = None

auto_draw_delay_job = None
auto_draw_progress_job = None
auto_draw_finalize_job = None

reset_progress_job = None
reset_finalize_job = None

# =========================================================
# DATA / STORAGE
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

        "start_time": None,

        "live_message_id": None,
        "closed_message_id": None,
        "selecting_message_id": None,
        "winners_message_id": None,

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify targets
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # old winner protection mode
        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},          # uid -> {"username": "@x"} used ONLY if mode=block

        # first join winner
        "first_winner_id": None,     # str uid
        "first_winner_username": "", # "@user"
        "first_winner_name": "",     # full name

        # winners final
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # claim window
        "claim_start_ts": None,
        "claim_expires_ts": None,

        # auto winner
        "auto_winner_on": False,

        # winners history
        # list of {"uid","username","prize","title","ts"}
        "winner_history": [],
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

# =========================================================
# HELPERS
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


def participants_count() -> int:
    return len(data.get("participants", {}))


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def bd_now_dt() -> datetime:
    return datetime.utcnow() + BD_OFFSET


def format_bd_datetime(ts: float) -> str:
    try:
        dt = datetime.utcfromtimestamp(float(ts)) + BD_OFFSET
    except Exception:
        dt = bd_now_dt()
    return dt.strftime("%d %B %Y • %I:%M %p")


def format_hms_colon(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_hms_spaced(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d} : {m:02d} : {s:02d}"


def build_bar(percent: int, blocks: int = 10) -> str:
    percent = max(0, min(100, int(percent)))
    filled = int(round(blocks * percent / 100.0))
    empty = blocks - filled
    return "▰" * filled + "▱" * empty


def parse_duration(text: str) -> int:
    t = (text or "").strip().lower()
    if not t:
        return 0
    parts = t.split()
    if len(parts) == 1 and parts[0].isdigit():
        return int(parts[0])

    if not parts or not parts[0].isdigit():
        return 0

    num = int(parts[0])
    unit = "".join(parts[1:]).strip()

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
    # If user already put emojis, keep as-is, else auto add bullets
    out = []
    for l in lines:
        out.append(l)
    return "\n".join(out)


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


def verify_user_join(bot, user_id: int) -> bool:
    targets = data.get("verify_targets", []) or []
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


def extract_primary_prize() -> str:
    """
    Prize input can be:
    "10× ChatGPT Plus"
    "🏆 10x ChatGPT PREMIUM"
    "10x ChatGPT PREMIUM\nBonus: 5x YouTube Premium"
    -> Return clean main prize name (first meaningful line) without multiplier.
    """
    raw = (data.get("prize") or "").strip()
    if not raw:
        return "Prize"

    first_line = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # skip "Bonus:" lines
        if line.lower().startswith("bonus"):
            continue
        first_line = line
        break

    if not first_line:
        first_line = raw.splitlines()[0].strip()

    # remove leading emojis / symbols
    first_line = re.sub(r"^[^\w\d]*", "", first_line).strip()

    # remove multiplier like "10x " or "10× "
    first_line = re.sub(r"^\d+\s*[x×]\s*", "", first_line, flags=re.IGNORECASE).strip()

    # normalize spacing
    return first_line if first_line else "Prize"


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
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ ON", callback_data="autowinner_on"),
            InlineKeyboardButton("❌ OFF", callback_data="autowinner_off"),
        ]]
    )


def reset_confirm_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ CONFIRM RESET", callback_data="reset_confirm"),
            InlineKeyboardButton("❌ REJECT", callback_data="reset_cancel"),
        ]]
    )

# =========================================================
# POPUPS
# =========================================================
def popup_verify_required() -> str:
    return (
        "🚫 VERIFICATION REQUIRED\n"
        "To join this giveaway, you must join the required channels/groups first ✅\n"
        "👇 After joining all of them, click JOIN GIVEAWAY again."
    )


def popup_old_winner_blocked() -> str:
    return (
        "🚫You have already won a previous giveaway.\n"
        "To keep the giveaway fair for everyone,\n"
        "repeat winners are restricted from participating.\n"
        "🙏Please wait for the next Giveaway"
    )


def popup_first_winner(username: str, uid: str) -> str:
    # MUST repeat same popup every time first winner taps
    return (
        "✨CONGRATULATIONS🌟\n"
        "You joined the giveaway FIRST and secured the🥇1st Winner spot!\n"
        f"👤 {username} | 🆔 {uid}\n"
        "📸 Screenshot & post in the group to confirm."
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY Unsuccessful\n"
        "You’ve already joined\n"
        "this giveaway 🎁\n\n"
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
    # admin contact MUST be easy copyable -> keep as plain text line
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        "If you believe this is a mistake, contact admin:\n"
        f"{ADMIN_CONTACT}"
    )


def popup_claim_winner(username: str, uid: str) -> str:
    prize_name = extract_primary_prize()
    return (
        "🌟 CONGRATULATIONS! ✨\n\n"
        "You’re an official winner of this giveaway 🏆\n\n"
        f"👤 {username} | 🆔 {uid}\n\n"
        "🎁 PRIZE WON\n"
        f"🏆 {prize_name}\n\n"
        "📩 Claim your prize — contact admin:\n"
        f"{ADMIN_CONTACT}"
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
# GIVEAWAY TEXT BUILDERS (YOUR STYLE)
# =========================================================
def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️🔥 {data.get('title','POWER POINT BREAK GIVEAWAY')} 🔥⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{(data.get('prize') or '').strip()}\n\n"
        f"👥 TOTAL PARTICIPANTS: 0\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms_spaced(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_bar(0)} 0%\n\n"
        "📜 RULES\n"
        f"{format_rules()}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = max(0, duration - remaining)
    percent = int(round((elapsed / float(duration)) * 100))
    percent = max(0, min(100, percent))

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡️🔥 {data.get('title','POWER POINT BREAK GIVEAWAY')} 🔥⚡️\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{(data.get('prize') or '').strip()}\n\n"
        f"👥 TOTAL PARTICIPANTS: {participants_count()}\n"
        f"🏅 TOTAL WINNERS: {data.get('winner_count',0)}\n"
        "🎯 WINNER SELECTION: 100% Random & Fair\n\n"
        "⏳ TIME REMAINING\n"
        f"🕒 {format_hms_spaced(remaining)}\n\n"
        "📊 LIVE PROGRESS\n"
        f"{build_bar(percent)} {percent}%\n\n"
        "📜 RULES\n"
        f"{format_rules()}\n\n"
        "📢 HOSTED BY\n"
        f"⚡️ {HOST_NAME} ⚡️\n\n"
        "👇✨ TAP THE BUTTON BELOW & JOIN NOW 👇"
    )


SPINNER = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]


def build_closed_post_text(spin: str = "⠋") -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY HAS ENDED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway window is officially closed.\n"
        "🔒 All entries are now final and locked.\n\n"
        f"👥 Participants: {participants_count()}\n"
        f"🏆 Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner selection is underway\n"
        f"{spin} Please stay tuned for the official announcement.\n\n"
        f"— {HOST_NAME} ⚡"
    )


def build_winners_post_text(first_uid: str, first_user: str, random_winners: list) -> str:
    prize_name = extract_primary_prize()
    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆✨GIVEAWAY WINNERS ANNOUNCEMENT🏆")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🎉 The wait is over!")
    lines.append("Here are the official winners of today’s giveaway 👇")
    lines.append("")
    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        lines.append(f"👑 Username: {first_user}")
    else:
        lines.append("👑 Username: (no username)")
    lines.append(f"🆔 User ID: {first_uid}")
    lines.append("⚡ Secured instantly by joining first")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("👑 OTHER WINNERS (RANDOMLY SELECTED)")
    i = 1
    for uid, uname in random_winners:
        if uname:
            lines.append(f"{i}️⃣ 👤 {uname}  | 🆔 {uid}")
        else:
            lines.append(f"{i}️⃣ 👤 User ID: {uid}")
        i += 1
    lines.append("")
    lines.append("🎁 PRIZE")
    lines.append(f"🏆 {prize_name}")
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


# =========================================================
# JOB CONTROL
# =========================================================
def stop_job_safe(job):
    if job is None:
        return
    try:
        job.schedule_removal()
    except Exception:
        pass


def stop_live_countdown():
    global countdown_job
    stop_job_safe(countdown_job)
    countdown_job = None


def stop_closed_spinner():
    global closed_spinner_job
    stop_job_safe(closed_spinner_job)
    closed_spinner_job = None


def stop_draw_jobs():
    global draw_job, draw_finalize_job
    stop_job_safe(draw_job)
    stop_job_safe(draw_finalize_job)
    draw_job = None
    draw_finalize_job = None


def stop_auto_draw_jobs():
    global auto_draw_delay_job, auto_draw_progress_job, auto_draw_finalize_job
    stop_job_safe(auto_draw_delay_job)
    stop_job_safe(auto_draw_progress_job)
    stop_job_safe(auto_draw_finalize_job)
    auto_draw_delay_job = None
    auto_draw_progress_job = None
    auto_draw_finalize_job = None


def stop_reset_jobs():
    global reset_progress_job, reset_finalize_job
    stop_job_safe(reset_progress_job)
    stop_job_safe(reset_finalize_job)
    reset_progress_job = None
    reset_finalize_job = None


# =========================================================
# LIVE COUNTDOWN (CHANNEL POST UPDATE)
# =========================================================
def start_live_countdown(job_queue):
    global countdown_job
    stop_live_countdown()
    countdown_job = job_queue.run_repeating(live_tick, interval=5, first=0, name="live_countdown")


def live_tick(context: CallbackContext):
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
            # Close giveaway
            data["active"] = False
            data["closed"] = True
            save_data()

            # delete live giveaway post
            if live_mid:
                try:
                    context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
                except Exception:
                    pass
                data["live_message_id"] = None
                save_data()

            # post closed message (with spinner)
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(SPINNER[0]))
                data["closed_message_id"] = m.message_id
                save_data()
                start_closed_spinner(context.job_queue)
            except Exception:
                pass

            # notify admin + auto winner status
            try:
                if data.get("auto_winner_on"):
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⏰ Giveaway Closed!\n"
                            "Auto winner is ON ✅\n\n"
                            "⏳ Auto selection will start soon."
                        ),
                    )
                    schedule_auto_draw(context.job_queue)
                else:
                    context.bot.send_message(
                        chat_id=ADMIN_ID,
                        text=(
                            "⏰ Giveaway Closed!\n"
                            "Auto winner is OFF ❌\n\n"
                            "Now use /draw to select winners."
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


# =========================================================
# CLOSED SPINNER ANIMATION (NO DOTS)
# =========================================================
def start_closed_spinner(job_queue):
    global closed_spinner_job
    stop_closed_spinner()
    closed_spinner_job = job_queue.run_repeating(
        closed_spinner_tick,
        interval=0.7,
        first=0,
        context={"tick": 0},
        name="closed_spinner",
    )


def closed_spinner_tick(context: CallbackContext):
    # stop if selecting or winners already posted
    if data.get("selecting_message_id") or data.get("winners_message_id"):
        stop_closed_spinner()
        return

    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_spinner()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    spin = SPINNER[(tick - 1) % len(SPINNER)]
    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(spin)
        )
    except Exception:
        pass


# =========================================================
# DRAW (MANUAL /draw) -> ADMIN PROGRESS 40s
# =========================================================
DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 5  # every 5 sec as you want


def build_draw_progress_text(percent: int) -> str:
    bar = build_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Progress: {bar} {percent}%\n\n"
        "✅ This draw is 100% fair & random.\n"
        "🔐 User ID based selection only.\n"
    )


def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job
    stop_draw_jobs()

    msg = context.bot.send_message(chat_id=admin_chat_id, text=build_draw_progress_text(0))
    ctx = {
        "admin_chat_id": admin_chat_id,
        "admin_msg_id": msg.message_id,
        "start_ts": now_ts(),
        "tick": 0,
    }

    def draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int((elapsed / float(DRAW_DURATION_SECONDS)) * 100)
        percent = max(0, min(100, percent))
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent),
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
        when=DRAW_DURATION_SECONDS,
        context=ctx,
        name="draw_finalize_job",
    )


def select_winners_build_text():
    participants = data.get("participants", {}) or {}
    if not participants:
        return None, None, None

    winner_count = int(data.get("winner_count", 1)) or 1
    winner_count = max(1, winner_count)

    first_uid = data.get("first_winner_id")
    if not first_uid:
        # fallback: first participant
        first_uid = next(iter(participants.keys()))
        info = participants.get(first_uid, {}) or {}
        data["first_winner_id"] = first_uid
        data["first_winner_username"] = info.get("username", "")
        data["first_winner_name"] = info.get("name", "")

    first_uname = data.get("first_winner_username", "") or (participants.get(first_uid, {}) or {}).get("username", "")

    pool = [uid for uid in participants.keys() if uid != first_uid]
    remaining_needed = max(0, winner_count - 1)
    remaining_needed = min(remaining_needed, len(pool))
    selected = random.sample(pool, remaining_needed) if remaining_needed > 0 else []

    winners_map = {}
    winners_map[first_uid] = {"username": first_uname}

    random_list = []
    for uid in selected:
        info = participants.get(uid, {}) or {}
        winners_map[uid] = {"username": info.get("username", "")}
        random_list.append((uid, info.get("username", "")))

    pending_text = build_winners_post_text(first_uid, first_uname, random_list)
    return winners_map, pending_text, random_list


def push_winner_history(winners_map: dict):
    prize_name = extract_primary_prize()
    title = (data.get("title") or "").strip() or "Giveaway"
    ts = now_ts()
    hist = data.get("winner_history", []) or []
    for uid, info in winners_map.items():
        hist.append({
            "uid": str(uid),
            "username": (info or {}).get("username", "") or "",
            "prize": prize_name,
            "title": title,
            "ts": ts
        })
    data["winner_history"] = hist


def draw_finalize(context: CallbackContext):
    global data
    stop_draw_jobs()

    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No participants to draw winners from.",
                )
            except Exception:
                pass
            return

        winners_map, pending_text, _ = select_winners_build_text()
        if not winners_map:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_chat_id,
                    message_id=admin_msg_id,
                    text="No participants to draw winners from.",
                )
            except Exception:
                pass
            return

        data["winners"] = winners_map
        data["pending_winners_text"] = pending_text
        save_data()

    # show preview for approve
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


# =========================================================
# AUTO WINNER (ON/OFF)
# - when giveaway ends, it posts ended msg with spinner
# - after 2 minutes remove ended msg
# - then 3 minutes progress in channel (every 5 sec)
# - then auto post winners in channel + claim button
# - previous winners post auto delete
# =========================================================
AUTO_CLOSE_REMOVE_AFTER = 120  # 2 minutes
AUTO_DRAW_DURATION = 180       # 3 minutes
AUTO_DRAW_INTERVAL = 5         # every 5 seconds


def schedule_auto_draw(job_queue):
    global auto_draw_delay_job
    stop_auto_draw_jobs()

    # after 2 minutes, remove closed message and start selecting
    auto_draw_delay_job = job_queue.run_once(auto_draw_start, when=AUTO_CLOSE_REMOVE_AFTER, name="auto_draw_delay")


def build_auto_select_progress_text(percent: int) -> str:
    bar = build_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 WINNER SELECTION (AUTO)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Progress: {bar} {percent}%\n\n"
        "🔐 100% Random & Fair\n"
        "Please wait...\n"
    )


def auto_draw_start(context: CallbackContext):
    global auto_draw_progress_job, auto_draw_finalize_job

    # remove closed msg
    with lock:
        closed_mid = data.get("closed_message_id")
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

    stop_closed_spinner()

    # remove any previous winners post (as you want)
    with lock:
        prev_win_mid = data.get("winners_message_id")
    if prev_win_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=prev_win_mid)
        except Exception:
            pass
        with lock:
            data["winners_message_id"] = None
            save_data()

    # post selecting message
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_auto_select_progress_text(0))
        with lock:
            data["selecting_message_id"] = m.message_id
            save_data()
    except Exception:
        return

    ctx = {"start_ts": now_ts(), "mid": data.get("selecting_message_id")}

    def auto_progress_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int((elapsed / float(AUTO_DRAW_DURATION)) * 100)
        percent = max(0, min(100, percent))
        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=jd["mid"],
                text=build_auto_select_progress_text(percent),
            )
        except Exception:
            pass

    auto_draw_progress_job = context.job_queue.run_repeating(
        auto_progress_tick,
        interval=AUTO_DRAW_INTERVAL,
        first=0,
        context=ctx,
        name="auto_draw_progress",
    )

    auto_draw_finalize_job = context.job_queue.run_once(
        auto_draw_finalize,
        when=AUTO_DRAW_DURATION,
        context=ctx,
        name="auto_draw_finalize",
    )


def auto_draw_finalize(context: CallbackContext):
    global data
    stop_auto_draw_jobs()

    # delete selecting progress msg
    with lock:
        sel_mid = data.get("selecting_message_id")
    if sel_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=sel_mid)
        except Exception:
            pass
        with lock:
            data["selecting_message_id"] = None
            save_data()

    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            # no one to draw
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text="Auto draw failed: No participants.")
            except Exception:
                pass
            return

        winners_map, winners_text, _ = select_winners_build_text()
        if not winners_map:
            try:
                context.bot.send_message(chat_id=ADMIN_ID, text="Auto draw failed: No participants.")
            except Exception:
                pass
            return

        data["winners"] = winners_map
        data["pending_winners_text"] = ""
        save_data()

    # post winners in channel
    try:
        m = context.bot.send_message(chat_id=CHANNEL_ID, text=winners_text, reply_markup=claim_button_markup())
        with lock:
            data["winners_message_id"] = m.message_id

            ts = now_ts()
            data["claim_start_ts"] = ts
            data["claim_expires_ts"] = ts + 24 * 3600

            # save history automatically (your request)
            push_winner_history(winners_map)

            save_data()
    except Exception as e:
        try:
            context.bot.send_message(chat_id=ADMIN_ID, text=f"Auto post winners failed: {e}")
        except Exception:
            pass


# =========================================================
# RESET PROGRESS (40s, every 5s, bar + %)
# =========================================================
RESET_DURATION_SECONDS = 40
RESET_INTERVAL = 5


def build_reset_progress_text(percent: int) -> str:
    bar = build_bar(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "♻️ FULL RESET IN PROGRESS\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 Progress: {bar} {percent}%\n\n"
        "⚠️ This will remove EVERYTHING.\n"
        "Please wait...\n"
    )


def start_reset_progress(context: CallbackContext, admin_chat_id: int, msg_id: int):
    global reset_progress_job, reset_finalize_job
    stop_reset_jobs()

    ctx = {"admin_chat_id": admin_chat_id, "msg_id": msg_id, "start_ts": now_ts()}

    def reset_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int((elapsed / float(RESET_DURATION_SECONDS)) * 100)
        percent = max(0, min(100, percent))
        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["msg_id"],
                text=build_reset_progress_text(percent),
            )
        except Exception:
            pass

    reset_progress_job = context.job_queue.run_repeating(
        reset_tick,
        interval=RESET_INTERVAL,
        first=0,
        context=ctx,
        name="reset_progress",
    )

    reset_finalize_job = context.job_queue.run_once(
        do_full_reset_everything,
        when=RESET_DURATION_SECONDS,
        context=ctx,
        name="reset_finalize",
    )


def do_full_reset_everything(context: CallbackContext):
    global data
    stop_live_countdown()
    stop_draw_jobs()
    stop_closed_spinner()
    stop_auto_draw_jobs()
    stop_reset_jobs()

    admin_chat_id = context.job.context.get("admin_chat_id")
    msg_id = context.job.context.get("msg_id")

    # delete channel messages best effort
    with lock:
        mids = [
            data.get("live_message_id"),
            data.get("closed_message_id"),
            data.get("selecting_message_id"),
            data.get("winners_message_id"),
        ]
    for mid in mids:
        if mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
            except Exception:
                pass

    with lock:
        data = fresh_default_data()
        save_data()

    try:
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=msg_id,
            text=(
                "━━━━━━━━━━━━━━━━━━━━\n"
                "✅ RESET COMPLETED SUCCESSFULLY!\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "Start again with:\n"
                "/newgiveaway"
            ),
        )
    except Exception:
        try:
            context.bot.send_message(chat_id=admin_chat_id, text="✅ Reset completed. Use /newgiveaway")
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
            "🚀 Giveaway Engine: READY\n"
            "🔐 Security Level: MAXIMUM\n\n"
            "🧭 Open the Admin Control Panel:\n"
            "/panel\n\n"
            f"⚡ POWERED BY: {HOST_NAME}"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join our official channel and wait for the giveaway post.\n\n"
            "🔗 Official Channel:\n"
            f"{CHANNEL_LINK}"
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL – POWER POINT BREAK\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/draw\n"
        "/endgiveaway\n\n"
        "⚡ AUTO WINNER\n"
        "/autowinnerpost\n\n"
        "🔒 BLOCK SYSTEM\n"
        "/blockpermanent\n"
        "/unban\n"
        "/blocklist\n"
        "/removeban\n\n"
        "✅ VERIFY SYSTEM\n"
        "/addverifylink\n"
        "/removeverifylink\n\n"
        "🏆 HISTORY\n"
        "/winnerlist\n\n"
        "♻️ RESET\n"
        "/reset"
    )


def cmd_autowinnerpost(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    status = "ON ✅" if data.get("auto_winner_on") else "OFF ❌"
    update.message.reply_text(
        f"⚡ AUTO WINNER POST SETTINGS\n\nCurrent: {status}\n\nChoose:",
        reply_markup=autowinner_markup()
    )


def cmd_winnerlist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    hist = data.get("winner_history", []) or []
    if not hist:
        update.message.reply_text("🏆 Winner History is empty.")
        return

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🏆 WINNER HISTORY LIST")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"Total Records: {len(hist)}")
    lines.append("")

    # newest first
    hist_sorted = sorted(hist, key=lambda x: float(x.get("ts", 0)), reverse=True)

    i = 1
    for r in hist_sorted[:200]:
        uid = str(r.get("uid", ""))
        uname = r.get("username", "") or "(no username)"
        prize = r.get("prize", "Prize")
        title = r.get("title", "Giveaway")
        ts = float(r.get("ts", now_ts()))
        dt = format_bd_datetime(ts)
        lines.append(f"{i}) 👤 {uname} | 🆔 {uid}")
        lines.append(f"   🎁 Prize: {prize}")
        lines.append(f"   ⚡ Giveaway: {title}")
        lines.append(f"   🗓 Date: {dt}")
        lines.append("")
        i += 1

    update.message.reply_text("\n".join(lines))


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY (CHAT ID / @USERNAME)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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
        "━━━━━━━━━━━━━━━━━━━━",
        "🗑 REMOVE VERIFY TARGET",
        "━━━━━━━━━━━━━━━━━━━━",
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
    stop_closed_spinner()
    stop_auto_draw_jobs()

    with lock:
        # keep verify + perma from previous? (you didn't request keep)
        # But safer: keep them
        keep_perma = data.get("permanent_block", {})
        keep_verify = data.get("verify_targets", {})
        keep_history = data.get("winner_history", [])

        data.clear()
        data.update(fresh_default_data())
        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify if isinstance(keep_verify, list) else []
        data["winner_history"] = keep_history
        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP STARTED\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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
        "━━━━━━━━━━━━━━━━━━━━",
        "👥 PARTICIPANTS LIST (ADMIN VIEW)",
        "━━━━━━━━━━━━━━━━━━━━",
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
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ END GIVEAWAY CONFIRMATION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 PERMANENT BLOCK\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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
            InlineKeyboardButton("Unban Old Winner Block", callback_data="unban_oldwinner"),
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
            InlineKeyboardButton("Reset Old Winner Ban List", callback_data="reset_oldwinner_ban"),
        ]]
    )
    update.message.reply_text("Choose which ban list to reset:", reply_markup=kb)


def cmd_blocklist(update: Update, context: CallbackContext):
    if not is_admin(update):
        return

    perma = data.get("permanent_block", {}) or {}
    oldw = data.get("old_winners", {}) or {}

    lines = []
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("📌 BAN LISTS")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("")
    lines.append(f"OLD WINNER MODE: {data.get('old_winner_mode','skip').upper()}")
    lines.append("")

    lines.append("⛔ OLD WINNER BLOCK LIST")
    lines.append(f"Total: {len(oldw)}")
    if oldw:
        i = 1
        for uid, info in oldw.items():
            uname = (info or {}).get("username", "")
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
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
            if uname:
                lines.append(f"{i}) {uname} | User ID: {uid}")
            else:
                lines.append(f"{i}) User ID: {uid}")
            i += 1
    else:
        lines.append("No permanently blocked users.")

    update.message.reply_text("\n".join(lines))


def cmd_reset(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "⚠️ FULL RESET?\n\nThis will remove EVERYTHING.\nConfirm?",
        reply_markup=reset_confirm_markup()
    )


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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET ADDED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Added: {ref}\n"
            f"Total: {len(data.get('verify_targets', []) or [])}\n\n"
            "What next?",
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
                update.message.reply_text("✅ All verify targets removed!")
                return

            if n < 1 or n > len(targets):
                update.message.reply_text("Invalid number. Try again.")
                return

            removed = targets.pop(n - 1)
            data["verify_targets"] = targets
            save_data()

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ VERIFY TARGET REMOVED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Removed: {removed.get('display','')}\n"
            f"Remaining: {len(data.get('verify_targets', []) or [])}"
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER PROTECTION MODE\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
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
                "📌 Old Winner Mode set to: SKIP\n\nNow send Giveaway Rules (multi-line):"
            )
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST SETUP\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ OLD WINNER BLOCK LIST SAVED!\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 Total Added: {len(data['old_winners']) - before}\n\n"
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
                update.message.reply_text("✅ Unbanned from Permanent Block!")
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
                update.message.reply_text("✅ Unbanned from Old Winner Block!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block list.")
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

    # ---- AUTO WINNER ON/OFF
    if qd in ("autowinner_on", "autowinner_off"):
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
            data["auto_winner_on"] = (qd == "autowinner_on")
            save_data()
        status = "ON ✅" if data["auto_winner_on"] else "OFF ❌"
        try:
            query.edit_message_text(f"✅ Auto winner set to: {status}")
        except Exception:
            pass
        return

    # Verify buttons
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
                "━━━━━━━━━━━━━━━━━━━━\n"
                "✅ VERIFY SETUP COMPLETED\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}\n"
                "All users must join ALL targets to join giveaway."
            )
        except Exception:
            pass
        return

    # Preview actions
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
                    data["selecting_message_id"] = None
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

                stop_closed_spinner()
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

    # End giveaway confirm/cancel
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

        # delete live message
        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass
            with lock:
                data["live_message_id"] = None
                save_data()

        # post ended msg (spinner)
        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text(SPINNER[0]))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
            start_closed_spinner(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()

        # if auto winner ON -> schedule
        try:
            if data.get("auto_winner_on"):
                schedule_auto_draw(context.job_queue)
                query.edit_message_text("✅ Giveaway Closed! Auto winner is ON ✅")
            else:
                query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw")
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

    # Reset confirm/cancel (progress reset)
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

        # start reset progress (40s) on same message
        try:
            query.edit_message_text(build_reset_progress_text(0))
        except Exception:
            pass
        start_reset_progress(context, query.message.chat_id, query.message.message_id)
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

    # Unban choose
    if qd == "unban_permanent":
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
        admin_state = "unban_permanent_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Permanent Block:")
        except Exception:
            pass
        return

    if qd == "unban_oldwinner":
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
        admin_state = "unban_oldwinner_input"
        try:
            query.edit_message_text("Send User ID (or @name | id) to unban from Old Winner Block:")
        except Exception:
            pass
        return

    # removeban choose confirm
    if qd in ("reset_permanent_ban", "reset_oldwinner_ban"):
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

        if qd == "reset_permanent_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Permanent", callback_data="confirm_reset_permanent"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try:
                query.edit_message_text("Confirm reset Permanent Ban List?", reply_markup=kb)
            except Exception:
                pass
            return

        if qd == "reset_oldwinner_ban":
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Confirm Reset Old Winner", callback_data="confirm_reset_oldwinner"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_reset_ban"),
                ]]
            )
            try:
                query.edit_message_text("Confirm reset Old Winner Ban List?", reply_markup=kb)
            except Exception:
                pass
            return

    if qd == "cancel_reset_ban":
        try:
            query.answer()
        except Exception:
            pass
        admin_state = None
        try:
            query.edit_message_text("Cancelled.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_permanent":
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
            data["permanent_block"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Permanent Ban List has been reset.")
        except Exception:
            pass
        return

    if qd == "confirm_reset_oldwinner":
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
            data["old_winners"] = {}
            save_data()
        try:
            query.edit_message_text("✅ Old Winner Ban List has been reset.")
        except Exception:
            pass
        return

    # Join giveaway
    if qd == "join_giveaway":
        if not data.get("active"):
            try:
                query.answer("This giveaway is not active right now.", show_alert=True)
            except Exception:
                pass
            return

        # verify
        if not verify_user_join(context.bot, int(uid)):
            try:
                query.answer(popup_verify_required(), show_alert=True)
            except Exception:
                pass
            return

        # permanent block
        if uid in (data.get("permanent_block", {}) or {}):
            try:
                query.answer(popup_permanent_blocked(), show_alert=True)
            except Exception:
                pass
            return

        # old winner block
        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        # if already first winner -> ALWAYS show first popup (not unsuccessful)
        first_uid = data.get("first_winner_id")
        if first_uid and uid == str(first_uid):
            tg_user = query.from_user
            uname = user_tag(tg_user.username or "") or data.get("first_winner_username", "") or "@username"
            try:
                query.answer(popup_first_winner(uname, uid), show_alert=True)
            except Exception:
                pass
            return

        # if already joined (not first) -> show already joined popup
        if uid in (data.get("participants", {}) or {}):
            try:
                query.answer(popup_already_joined(), show_alert=True)
            except Exception:
                pass
            return

        # add participant
        tg_user = query.from_user
        uname = user_tag(tg_user.username or "")
        full_name = (tg_user.full_name or "").strip()

        with lock:
            # set first winner if none
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
                remaining = max(0, duration - elapsed)
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=live_mid,
                    text=build_live_text(remaining),
                    reply_markup=join_button_markup(),
                )
        except Exception:
            pass

        # show correct popup
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

    # Winners Approve/Reject (manual draw)
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

        # remove closed message if exists (your request)
        closed_mid = data.get("closed_message_id")
        if closed_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
            except Exception:
                pass
            with lock:
                data["closed_message_id"] = None
                save_data()

        stop_closed_spinner()

        # delete previous winners post if exists
        prev_mid = data.get("winners_message_id")
        if prev_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=prev_mid)
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

                ts = now_ts()
                data["claim_start_ts"] = ts
                data["claim_expires_ts"] = ts + 24 * 3600

                # save history automatically
                push_winner_history(data.get("winners", {}) or {})

                data["pending_winners_text"] = ""
                save_data()

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

    # Claim prize
    if qd == "claim_prize":
        winners = data.get("winners", {}) or {}

        if uid not in winners:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        exp_ts = data.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        uname = winners.get(uid, {}).get("username", "") or "@username"
        try:
            query.answer(popup_claim_winner(uname, uid), show_alert=True)
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
    if ADMIN_ID == 0 or CHANNEL_ID == 0:
        raise SystemExit("ADMIN_ID or CHANNEL_ID missing in .env")

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # basic
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("panel", cmd_panel))

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # auto winner
    dp.add_handler(CommandHandler("autowinnerpost", cmd_autowinnerpost))

    # bans
    dp.add_handler(CommandHandler("blockpermanent", cmd_blockpermanent))
    dp.add_handler(CommandHandler("unban", cmd_unban))
    dp.add_handler(CommandHandler("removeban", cmd_removeban))
    dp.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # history
    dp.add_handler(CommandHandler("winnerlist", cmd_winnerlist))

    # reset
    dp.add_handler(CommandHandler("reset", cmd_reset))

    # handlers
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, admin_text_handler))
    dp.add_handler(CallbackQueryHandler(cb_handler))

    # Resume after restart (best effort)
    if data.get("active"):
        start_live_countdown(updater.job_queue)

    if data.get("closed") and data.get("closed_message_id") and not data.get("selecting_message_id") and not data.get("winners_message_id"):
        start_closed_spinner(updater.job_queue)

    # If auto winner was ON and closed but not selected yet -> schedule auto draw
    if data.get("closed") and data.get("auto_winner_on") and data.get("closed_message_id") and not data.get("winners_message_id") and not data.get("selecting_message_id"):
        schedule_auto_draw(updater.job_queue)

    print("Bot is running (PTB 13, GSM compatible, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
