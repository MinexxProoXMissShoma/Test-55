# bot.py (PTB v13 / non-async)
# ---------------------------------------------------------
# Features (Fully Fixed):
# ✅ Giveaway setup (Title/Prize/Winners/Duration/Rules)
# ✅ Verify join targets (multiple)
# ✅ Permanent block (kept across /newgiveaway + /reset)
# ✅ Old winner mode (BLOCK requires list, SKIP no list)
# ✅ First join champion popup (always same)
# ✅ Join already popup
# ✅ Giveaway auto close + closed animation (spinner/dots)
# ✅ Manual /draw (progress in admin -> auto announce in channel)
# ✅ AutoDraw ON/OFF (5 min, update every 5 sec, pin, show 1 entry username + user id)
# ✅ 100% complete -> auto delete CLOSED + AUTO DRAW post -> post Winners
# ✅ Multi giveaway Claim: each winners post has its own Giveaway ID (claim shows correct giveaway)
# ✅ /prizeDelivered (per giveaway ID): marks delivered, edits winners post, delivered claim popup shows "already delivered"
# ✅ Claim button auto expires after 24h (removes button on that specific winners post)
# ---------------------------------------------------------

import os
import json
import random
import threading
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

auto_draw_job = None
auto_draw_finalize_job = None
auto_draw_msg_id = None

claim_expire_job = None

# =========================================================
# CONSTANTS
# =========================================================
DOTS = [".", "..", "...", "....", ".....", "......", "......."]
SPINNER = ["🔄", "🔃", "🔁", "🔂"]

DRAW_DURATION_SECONDS = 40
DRAW_UPDATE_INTERVAL = 0.7

AUTO_DRAW_DURATION_SECONDS = 5 * 60
AUTO_DRAW_UPDATE_INTERVAL = 5.0
SHOW_COLORS = ["🟣", "🟠", "🟢", "🔵", "🟡", "🔴"]

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

        # participants
        "participants": {},  # uid(str) -> {"username": "@x" or "", "name": ""}

        # verify targets
        "verify_targets": [],  # [{"ref": "-100..." or "@xxx", "display": "..."}]

        # permanent block
        "permanent_block": {},  # uid -> {"username": "@x" or ""}

        # old winner protection mode
        "old_winner_mode": "skip",  # "block" or "skip"
        "old_winners": {},          # uid -> {"username": "@x" or ""} only if mode=block

        # first join winner
        "first_winner_id": None,     # str uid
        "first_winner_username": "", # "@user"
        "first_winner_name": "",

        # winners current
        "winners": {},  # uid -> {"username": "@x"}
        "pending_winners_text": "",

        # autodraw
        "auto_draw": False,

        # giveaway history (multi winners posts)
        "giveaway_seq": 0,
        "history": {},  # gid -> snapshot

        # reset-safe claim
        # (claim window info per giveaway stored in history snapshot)
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

    # fix types
    if not isinstance(d.get("verify_targets"), list):
        d["verify_targets"] = []
    if not isinstance(d.get("permanent_block"), dict):
        d["permanent_block"] = {}
    if not isinstance(d.get("old_winners"), dict):
        d["old_winners"] = {}
    if not isinstance(d.get("participants"), dict):
        d["participants"] = {}
    if not isinstance(d.get("history"), dict):
        d["history"] = {}

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


def format_hms(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_progress(percent: float) -> str:
    percent = max(0, min(100, percent))
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
            "• Must join the official channel\n"
            "• One entry per user only\n"
            "• Stay active until result announcement\n"
            "• Admin decision is final & binding"
        )
    lines = [l.strip() for l in rules.splitlines() if l.strip()]
    return "\n".join("• " + l for l in lines)


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


def make_gid() -> str:
    with lock:
        data["giveaway_seq"] = int(data.get("giveaway_seq", 0)) + 1
        save_data()
        return f"G-{data['giveaway_seq']:04d}"


# =========================================================
# MARKUPS
# =========================================================
def join_button_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🎁✨ JOIN GIVEAWAY NOW ✨🎁", callback_data="join_giveaway")]]
    )


def claim_button_markup(gid: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏆✨ CLAIM YOUR PRIZE NOW ✨🏆", callback_data=f"claim_prize|{gid}")]]
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


def autodraw_markup():
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ AutoDraw ON", callback_data="autodraw_on"),
            InlineKeyboardButton("❌ AutoDraw OFF", callback_data="autodraw_off"),
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
        "🚫 YOU ARE BLOCKED\n"
        "You have already won a previous giveaway.\n"
        "Repeat winners are restricted to keep it fair.\n"
        "🙏 Please wait for the next giveaway."
    )


def popup_first_winner(username: str, uid: str) -> str:
    return (
        "🥇 FIRST JOIN CHAMPION 🌟\n"
        "Congratulations! You joined\n"
        "the giveaway FIRST and secured\n"
        f"👤 {username}\n"
        f"🆔 {uid}\n"
        "📸 Please take a screenshot\n"
        "and post it in the group\n"
        "to confirm 👈"
    )


def popup_already_joined() -> str:
    return (
        "🚫 ENTRY UNSUCCESSFUL\n"
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
    return (
        "⛔ PERMANENTLY BLOCKED\n"
        "You are permanently blocked from joining giveaways.\n"
        f"If you believe this is a mistake, contact admin: {ADMIN_CONTACT}"
    )


def popup_claim_not_winner() -> str:
    return (
        "❌ YOU ARE NOT A WINNER\n\n"
        "Sorry! Your User ID is not\n"
        "in the winners list.\n\n"
        "Please wait for the next\n"
        "giveaway ❤️‍🩹"
    )


def popup_prize_expired() -> str:
    return (
        "⏳ PRIZE EXPIRED\n"
        "Your 24-hour claim time has ended.\n"
        "This prize is no longer available."
    )


# =========================================================
# TEXT BUILDERS
# =========================================================
def build_preview_text() -> str:
    remaining = data.get("duration_seconds", 0)
    progress = build_progress(0)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔍 GIVEAWAY PREVIEW (ADMIN)\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"⚡ {data.get('title','')} ⚡\n\n"
        "🎁 Prize:\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: 0\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair\n"
        "• Auto System\n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}\n"
        "📊 Live Progress\n"
        f"{progress}\n\n"
        "📜 Official Rules\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted by: {HOST_NAME}\n"
        f"🔗 Official Channel: {CHANNEL_USERNAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_live_text(remaining: int) -> str:
    duration = data.get("duration_seconds", 1) or 1
    elapsed = duration - remaining
    elapsed = max(0, min(duration, elapsed))
    percent = int(round((elapsed / float(duration)) * 100))
    progress = build_progress(percent)

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ {HOST_NAME} GIVEAWAY ⚡\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎁 PRIZE POOL 🌟\n"
        f"{data.get('prize','')}\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        "🎯 Winner Selection\n"
        "• 100% Random & Fair\n"
        "• Auto System\n\n"
        f"⏱️ Time Remaining: {format_hms(remaining)}\n"
        "📊 Live Progress\n"
        f"{progress}\n\n"
        "📜 Official Rules\n"
        f"{format_rules()}\n\n"
        f"📢 Hosted by: {HOST_NAME}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 Tap below to join the giveaway 👇"
    )


def build_closed_post_text(spin: str = "🔄", dots: str = "...") -> str:
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚫 GIVEAWAY OFFICIALLY CLOSED 🚫\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏰ The giveaway has officially ended.\n"
        "🔒 All entries are now closed.\n\n"
        f"👥 Total Participants: {participants_count()}\n"
        f"🏆 Total Winners: {data.get('winner_count',0)}\n\n"
        f"{spin} Winner selection is currently in progress{dots}\n"
        "Please wait for the official announcement.\n\n"
        f"🙏 Thank you to everyone who participated.\n"
        f"— {HOST_NAME} ⚡"
    )


def build_draw_progress_text(percent: int, spin: str, dots: str) -> str:
    bar = build_progress(percent)
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners: {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        "🔐 100% fair & random system\n"
        "🔒 User ID based selection only\n\n"
        f"Please wait{dots}"
    )


def build_auto_draw_text(percent: int, remaining: int, spin: str, dots: str, color: str, uname: str, uid: str) -> str:
    bar = build_progress(percent)

    if uname:
        show_line_1 = f"{color} ➤ Now showing: {uname}"
    else:
        show_line_1 = f"{color} ➤ Now showing: User"
    show_line_2 = f"    🆔 {uid}" if uid else "    🆔 ---"

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 AUTO RANDOM WINNER SELECTION\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{spin} Selecting winners: {percent}%\n"
        f"📊 Progress: {bar}\n\n"
        f"🕒 Time Remaining: {format_hms(remaining)}\n"
        "🔐 100% Random • Fair • Auto System\n\n"
        "👥 Live Entries Showcase\n"
        f"{show_line_1}\n"
        f"{show_line_2}\n\n"
        f"Please wait{dots}"
    )


def build_winners_post_text(gid: str, first_uid: str, first_user: str, random_winners: list, delivered: dict) -> str:
    delivered = delivered or {}
    total_winners = int(data.get("winner_count", 0)) or (1 + len(random_winners))
    delivered_count = sum(1 for k in delivered.keys() if delivered.get(k))

    lines = []
    lines.append("🏆 GIVEAWAY WINNERS ANNOUNCEMENT 🏆")
    lines.append("")
    lines.append(f"{HOST_NAME}")
    lines.append("")
    lines.append(f"🆔 Giveaway ID: {gid}")
    lines.append("")
    lines.append("🎁 PRIZE:")
    lines.append(f"{data.get('prize','')}")
    lines.append(f"📦 Prize Delivery: {delivered_count}/{total_winners}")
    lines.append("")

    lines.append("🥇 ⭐ FIRST JOIN CHAMPION ⭐")
    if first_user:
        flag = "✅ Delivered" if delivered.get(first_uid) else ""
        if flag:
            lines.append(f"👑 {first_user} | 🆔 {first_uid} | {flag}")
        else:
            lines.append(f"👑 {first_user}")
            lines.append(f"🆔 {first_uid}")
    else:
        flag = "✅ Delivered" if delivered.get(first_uid) else ""
        if flag:
            lines.append(f"👑 User | 🆔 {first_uid} | {flag}")
        else:
            lines.append("👑 User")
            lines.append(f"🆔 {first_uid}")
    lines.append("")

    lines.append("👑 OTHER WINNERS")
    i = 1
    for uid, uname in random_winners:
        flag = "✅ Delivered" if delivered.get(uid) else ""
        if uname:
            if flag:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid} | {flag}")
            else:
                lines.append(f"{i}️⃣ 👤 {uname} | 🆔 {uid}")
        else:
            if flag:
                lines.append(f"{i}️⃣ 👤 User | 🆔 {uid} | {flag}")
            else:
                lines.append(f"{i}️⃣ 👤 User | 🆔 {uid}")
        i += 1

    lines.append("")
    lines.append("👇 Click the button below to claim your prize")
    lines.append("")
    lines.append("⏳ Rule: Claim within 24 hours — after that, prize expires.")

    return "\n".join(lines)


# =========================================================
# JOBS: STOP HELPERS
# =========================================================
def stop_live_countdown():
    global countdown_job
    if countdown_job is not None:
        try:
            countdown_job.schedule_removal()
        except Exception:
            pass
    countdown_job = None


def stop_closed_anim():
    global closed_anim_job
    if closed_anim_job is not None:
        try:
            closed_anim_job.schedule_removal()
        except Exception:
            pass
    closed_anim_job = None


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


def stop_auto_draw_jobs():
    global auto_draw_job, auto_draw_finalize_job
    if auto_draw_job is not None:
        try:
            auto_draw_job.schedule_removal()
        except Exception:
            pass
    auto_draw_job = None

    if auto_draw_finalize_job is not None:
        try:
            auto_draw_finalize_job.schedule_removal()
        except Exception:
            pass
    auto_draw_finalize_job = None


def stop_claim_expire_job():
    global claim_expire_job
    if claim_expire_job is not None:
        try:
            claim_expire_job.schedule_removal()
        except Exception:
            pass
    claim_expire_job = None


# =========================================================
# LIVE COUNTDOWN
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

            # post closed message and save id
            try:
                m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text("🔄", "..."))
                data["closed_message_id"] = m.message_id
                save_data()
                start_closed_anim(context.job_queue)
            except Exception:
                pass

            # notify admin
            try:
                context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        "⏰ Giveaway Closed Automatically!\n\n"
                        f"Giveaway: {data.get('title','')}\n"
                        f"Total Participants: {participants_count()}\n\n"
                        "Manual: use /draw\n"
                        "AutoDraw: will run if enabled."
                    ),
                )
            except Exception:
                pass

            # auto draw start if enabled
            if data.get("auto_draw", False):
                try:
                    start_auto_draw(context)
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
# CLOSED ANIMATION
# =========================================================
def start_closed_anim(job_queue):
    global closed_anim_job
    stop_closed_anim()
    closed_anim_job = job_queue.run_repeating(
        closed_anim_tick,
        interval=0.7,
        first=0,
        context={"tick": 0},
        name="closed_anim",
    )


def closed_anim_tick(context: CallbackContext):
    mid = data.get("closed_message_id")
    if not mid:
        stop_closed_anim()
        return

    ctx = context.job.context or {}
    tick = int(ctx.get("tick", 0)) + 1
    ctx["tick"] = tick
    context.job.context = ctx

    spin = SPINNER[(tick - 1) % len(SPINNER)]
    dots = DOTS[(tick - 1) % len(DOTS)]

    try:
        context.bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=mid,
            text=build_closed_post_text(spin, dots),
        )
    except Exception:
        pass


# =========================================================
# WINNER SELECTION CORE
# =========================================================
def select_winners_from_participants():
    with lock:
        participants = data.get("participants", {}) or {}
        if not participants:
            return None

        winner_count = int(data.get("winner_count", 1)) or 1
        winner_count = max(1, winner_count)

        first_uid = data.get("first_winner_id")
        if not first_uid:
            first_uid = next(iter(participants.keys()))
            info = participants.get(first_uid, {}) or {}
            data["first_winner_id"] = first_uid
            data["first_winner_username"] = info.get("username", "")
            data["first_winner_name"] = info.get("name", "")
            save_data()

        first_uname = data.get("first_winner_username", "")
        if not first_uname:
            first_uname = (participants.get(first_uid, {}) or {}).get("username", "")

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
        save_data()

        return first_uid, first_uname, random_list


def snapshot_and_post_winners(context: CallbackContext):
    global auto_draw_msg_id

    sel = select_winners_from_participants()
    if not sel:
        return

    first_uid, first_uname, random_list = sel

    # create giveaway snapshot
    gid = make_gid()
    claim_start = now_ts()
    claim_expires = claim_start + 24 * 3600

    delivered = {}  # uid -> True

    winners_text = build_winners_post_text(
        gid=gid,
        first_uid=first_uid,
        first_user=first_uname,
        random_winners=random_list,
        delivered=delivered
    )

    # delete closed + auto draw msg
    closed_mid = data.get("closed_message_id")
    if closed_mid:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=closed_mid)
        except Exception:
            pass
        with lock:
            data["closed_message_id"] = None
            save_data()

    if auto_draw_msg_id:
        try:
            context.bot.delete_message(chat_id=CHANNEL_ID, message_id=auto_draw_msg_id)
        except Exception:
            pass
        auto_draw_msg_id = None

    # post winners to channel
    m = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=winners_text,
        reply_markup=claim_button_markup(gid),
    )

    # save snapshot in history
    with lock:
        hist = data.get("history", {}) or {}
        snap = {
            "gid": gid,
            "title": data.get("title", ""),
            "prize": data.get("prize", ""),
            "winner_count": int(data.get("winner_count", 0)) or (1 + len(random_list)),
            "winners": dict(data.get("winners", {}) or {}),
            "delivered": delivered,
            "created_ts": claim_start,
            "claim_expires_ts": claim_expires,
            "admin_contact": ADMIN_CONTACT,
            "host_name": HOST_NAME,
            "channel_id": CHANNEL_ID,
            "winners_message_id": m.message_id,
        }
        hist[gid] = snap
        data["history"] = hist
        save_data()

    schedule_claim_expire(context.job_queue, gid)


# =========================================================
# MANUAL DRAW (ADMIN PROGRESS -> AUTO ANNOUNCE)
# =========================================================
def start_draw_progress(context: CallbackContext, admin_chat_id: int):
    global draw_job, draw_finalize_job

    stop_draw_jobs()

    msg = context.bot.send_message(
        chat_id=admin_chat_id,
        text=build_draw_progress_text(0, SPINNER[0], "."),
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
        percent = int(round(min(100, (elapsed / float(DRAW_DURATION_SECONDS)) * 100)))

        spin = SPINNER[(tick - 1) % len(SPINNER)]
        dots = DOTS[(tick - 1) % len(DOTS)]

        try:
            job_ctx.bot.edit_message_text(
                chat_id=jd["admin_chat_id"],
                message_id=jd["admin_msg_id"],
                text=build_draw_progress_text(percent, spin, dots),
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


def draw_finalize(context: CallbackContext):
    stop_draw_jobs()

    jd = context.job.context
    admin_chat_id = jd["admin_chat_id"]
    admin_msg_id = jd["admin_msg_id"]

    # auto announce winners in channel
    try:
        snapshot_and_post_winners(context)
        stop_closed_anim()
        context.bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=admin_msg_id,
            text="✅ Draw completed successfully!\nWinners announced in channel.",
        )
    except Exception as e:
        try:
            context.bot.edit_message_text(
                chat_id=admin_chat_id,
                message_id=admin_msg_id,
                text=f"Draw failed: {e}",
            )
        except Exception:
            pass


# =========================================================
# AUTO DRAW (5 MIN, UPDATE EVERY 5 SEC, PIN)
# =========================================================
def start_auto_draw(context: CallbackContext):
    global auto_draw_job, auto_draw_finalize_job, auto_draw_msg_id

    stop_auto_draw_jobs()

    m = context.bot.send_message(
        chat_id=CHANNEL_ID,
        text=build_auto_draw_text(
            percent=0,
            remaining=AUTO_DRAW_DURATION_SECONDS,
            spin=SPINNER[0],
            dots=DOTS[0],
            color=SHOW_COLORS[0],
            uname="",
            uid="",
        ),
    )
    auto_draw_msg_id = m.message_id

    try:
        context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=auto_draw_msg_id, disable_notification=True)
    except Exception:
        pass

    ctx = {"start_ts": now_ts(), "tick": 0, "msg_id": auto_draw_msg_id, "show_index": 0}

    def auto_draw_tick(job_ctx: CallbackContext):
        jd = job_ctx.job.context
        tick = int(jd.get("tick", 0)) + 1
        jd["tick"] = tick

        elapsed = max(0.0, now_ts() - float(jd["start_ts"]))
        percent = int(round(min(100, (elapsed / float(AUTO_DRAW_DURATION_SECONDS)) * 100)))
        remaining = int(max(0, AUTO_DRAW_DURATION_SECONDS - elapsed))

        spin = SPINNER[(tick - 1) % len(SPINNER)]
        dots = DOTS[(tick - 1) % len(DOTS)]
        color = SHOW_COLORS[(tick - 1) % len(SHOW_COLORS)]

        with lock:
            participants = data.get("participants", {}) or {}
            keys = list(participants.keys())

        uname = ""
        uid = ""
        if keys:
            idx = int(jd.get("show_index", 0)) % len(keys)
            jd["show_index"] = idx + 1
            uid = keys[idx]
            info = participants.get(uid, {}) or {}
            uname = (info.get("username", "") or "").strip()

        try:
            job_ctx.bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=jd["msg_id"],
                text=build_auto_draw_text(percent, remaining, spin, dots, color, uname, uid),
            )
        except Exception:
            pass

    auto_draw_job = context.job_queue.run_repeating(
        auto_draw_tick,
        interval=AUTO_DRAW_UPDATE_INTERVAL,
        first=0,
        context=ctx,
        name="auto_draw_tick",
    )

    auto_draw_finalize_job = context.job_queue.run_once(
        auto_draw_finalize,
        when=AUTO_DRAW_DURATION_SECONDS,
        context=ctx,
        name="auto_draw_finalize",
    )


def auto_draw_finalize(context: CallbackContext):
    stop_auto_draw_jobs()
    try:
        snapshot_and_post_winners(context)
        stop_closed_anim()
    except Exception:
        pass


# =========================================================
# CLAIM BUTTON EXPIRY (PER GIVEAWAY ID)
# =========================================================
def schedule_claim_expire(job_queue, gid: str):
    stop_claim_expire_job()

    with lock:
        snap = (data.get("history", {}) or {}).get(gid)
        if not snap:
            return
        exp = snap.get("claim_expires_ts")
        mid = snap.get("winners_message_id")
        ch = snap.get("channel_id", CHANNEL_ID)

    if not exp or not mid:
        return

    remain = float(exp) - now_ts()
    if remain <= 0:
        return

    job_queue.run_once(
        expire_claim_button_job,
        when=remain,
        context={"gid": gid, "chat_id": ch, "message_id": mid},
        name=f"claim_expire_{gid}",
    )


def expire_claim_button_job(context: CallbackContext):
    ctx = context.job.context or {}
    chat_id = ctx.get("chat_id", CHANNEL_ID)
    message_id = ctx.get("message_id")
    if not message_id:
        return

    try:
        context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception:
        pass


# =========================================================
# COMMANDS
# =========================================================
def cmd_start(update: Update, context: CallbackContext):
    u = update.effective_user
    if u and u.id == ADMIN_ID:
        update.message.reply_text(
            "🛡️ ADMIN PANEL READY ✅\n\n"
            "Open:\n"
            "/panel"
        )
    else:
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ {HOST_NAME} Giveaway System ⚡\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Please join the official channel:\n"
            f"{CHANNEL_LINK}"
        )


def cmd_panel(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "🛠 ADMIN CONTROL PANEL\n\n"
        "📌 GIVEAWAY\n"
        "/newgiveaway\n"
        "/participants\n"
        "/endgiveaway\n"
        "/draw\n\n"
        "⚙️ AUTO DRAW\n"
        "/Autodraw\n\n"
        "📦 PRIZE DELIVERY\n"
        "/prizeDelivered\n\n"
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


def cmd_autodraw(update: Update, context: CallbackContext):
    if not is_admin(update):
        return
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎲 AUTO DRAW SETTINGS\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Choose option:",
        reply_markup=autodraw_markup(),
    )


def cmd_addverifylink(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "add_verify"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ ADD VERIFY TARGET\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Chat ID (recommended) OR @username:\n\n"
        "Examples:\n"
        "-1001234567890\n"
        "@PowerPointBreak"
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
    stop_closed_anim()
    stop_auto_draw_jobs()

    with lock:
        keep_perma = dict(data.get("permanent_block", {}) or {})
        keep_verify = list(data.get("verify_targets", []) or [])
        keep_auto = bool(data.get("auto_draw", False))
        keep_history = dict(data.get("history", {}) or {})
        keep_seq = int(data.get("giveaway_seq", 0))

        data.clear()
        data.update(fresh_default_data())

        data["permanent_block"] = keep_perma
        data["verify_targets"] = keep_verify
        data["auto_draw"] = keep_auto
        data["history"] = keep_history
        data["giveaway_seq"] = keep_seq

        save_data()

    admin_state = "title"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 NEW GIVEAWAY SETUP\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "STEP 1 — Send Giveaway Title:"
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
        "👥 PARTICIPANTS LIST",
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
        "⚠️ END GIVEAWAY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Are you sure you want to end now?",
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


def cmd_prize_delivered(update: Update, context: CallbackContext):
    global admin_state
    if not is_admin(update):
        return
    admin_state = "prize_delivered_gid"
    update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 PRIZE DELIVERY\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Send Giveaway ID first.\n"
        "Example:\n"
        "G-0001"
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
            f"Total Verify Targets: {len(data.get('verify_targets', []) or [])}",
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
            "5 Minute\n"
            "1 Hour"
        )
        return

    if admin_state == "duration":
        seconds = parse_duration(msg)
        if seconds <= 0:
            update.message.reply_text("Invalid duration. Example: 30 Second / 5 Minute / 1 Hour")
            return
        with lock:
            data["duration_seconds"] = seconds
            save_data()

        admin_state = "old_winner_mode"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 OLD WINNER MODE\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "1 → BLOCK OLD WINNERS\n"
            "2 → SKIP OLD WINNERS\n\n"
            "Reply with 1 or 2:"
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
            update.message.reply_text("Send Giveaway Rules (multi-line):")
            return

        with lock:
            data["old_winner_mode"] = "block"
            data["old_winners"] = {}
            save_data()

        admin_state = "old_winner_block_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ OLD WINNER BLOCK LIST\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send old winners list (one per line):\n\n"
            "Format:\n"
            "@username | user_id\n\n"
            "Example:\n"
            "@minexxproo | 728272\n"
            "@user2 | 889900\n"
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
            "✅ OLD WINNER LIST SAVED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"New Added: {len(data['old_winners']) - before}\n"
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
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ PERMANENT BLOCK SAVED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"New Added: {len(data['permanent_block']) - before}\n"
            f"Total Blocked: {len(data['permanent_block'])}"
        )
        return

    # UNBAN INPUTS
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
                update.message.reply_text("✅ Unbanned from Old Winner Block successfully!")
            else:
                update.message.reply_text("This user id is not in Old Winner Block list.")
        admin_state = None
        return

    # PRIZE DELIVERY FLOW
    if admin_state == "prize_delivered_gid":
        gid = msg.strip().upper()
        with lock:
            snap = (data.get("history", {}) or {}).get(gid)
        if not snap:
            update.message.reply_text("Giveaway ID not found. Example: G-0001")
            return
        context.user_data["prize_gid"] = gid
        admin_state = "prize_delivered_list"
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ GIVEAWAY FOUND\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Giveaway ID: {gid}\n\n"
            "Now send delivery list (one per line):\n"
            "@username | user_id\n"
            "or\n"
            "user_id"
        )
        return

    if admin_state == "prize_delivered_list":
        gid = context.user_data.get("prize_gid", "")
        entries = parse_user_lines(msg)
        if not entries:
            update.message.reply_text("Send valid list: user_id OR @name | user_id")
            return

        with lock:
            hist = data.get("history", {}) or {}
            snap = hist.get(gid)
            if not snap:
                admin_state = None
                update.message.reply_text("Giveaway snapshot missing.")
                return

            winners = snap.get("winners", {}) or {}
            delivered = snap.get("delivered", {}) or {}
            before = sum(1 for k in delivered if delivered.get(k))

            marked = 0
            for uid, uname in entries:
                if uid in winners:
                    delivered[uid] = True
                    # keep username best effort
                    if uname and uid in winners:
                        winners[uid]["username"] = uname
                    marked += 1

            snap["delivered"] = delivered
            snap["winners"] = winners
            hist[gid] = snap
            data["history"] = hist
            save_data()

        # edit winners post text
        try:
            first_uid = data.get("first_winner_id") or ""
            first_uname = data.get("first_winner_username") or ""
            # rebuild from snapshot winners order:
            # use current giveaway winners map for display: first + others
            with lock:
                snap2 = (data.get("history", {}) or {}).get(gid) or {}
                wmap = snap2.get("winners", {}) or {}
                delivered2 = snap2.get("delivered", {}) or {}

            # determine first winner from snapshot if possible
            if first_uid not in wmap and wmap:
                first_uid = list(wmap.keys())[0]
            first_uname = (wmap.get(first_uid, {}) or {}).get("username", first_uname)

            others = [(uid, (wmap.get(uid, {}) or {}).get("username", "")) for uid in wmap.keys() if uid != first_uid]

            text = build_winners_post_text(
                gid=gid,
                first_uid=first_uid,
                first_user=first_uname,
                random_winners=others,
                delivered=delivered2,
            )

            ch = snap2.get("channel_id", CHANNEL_ID)
            mid = snap2.get("winners_message_id")

            if mid:
                context.bot.edit_message_text(
                    chat_id=ch,
                    message_id=mid,
                    text=text,
                    reply_markup=claim_button_markup(gid),
                )
        except Exception:
            pass

        with lock:
            hist = data.get("history", {}) or {}
            snap3 = hist.get(gid) or {}
            delivered3 = snap3.get("delivered", {}) or {}
            after = sum(1 for k in delivered3 if delivered3.get(k))

        admin_state = None
        update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✅ PRIZE DELIVERY UPDATED\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Giveaway ID: {gid}\n"
            f"Marked Delivered: {marked}\n"
            f"Delivery Count: {after}\n"
        )
        return


# =========================================================
# CALLBACK HANDLER
# =========================================================
def cb_handler(update: Update, context: CallbackContext):
    global admin_state
    query = update.callback_query
    qd = query.data
    uid = str(query.from_user.id)

    # verify actions
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

    # AutoDraw ON/OFF
    if qd in ("autodraw_on", "autodraw_off"):
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
            data["auto_draw"] = (qd == "autodraw_on")
            save_data()

        try:
            query.edit_message_text(
                "✅ AutoDraw turned ON." if qd == "autodraw_on" else "❌ AutoDraw turned OFF."
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

                    data["participants"] = {}
                    data["winners"] = {}
                    data["pending_winners_text"] = ""
                    data["first_winner_id"] = None
                    data["first_winner_username"] = ""
                    data["first_winner_name"] = ""
                    save_data()

                stop_closed_anim()
                stop_auto_draw_jobs()
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

        live_mid = data.get("live_message_id")
        if live_mid:
            try:
                context.bot.delete_message(chat_id=CHANNEL_ID, message_id=live_mid)
            except Exception:
                pass

        try:
            m = context.bot.send_message(chat_id=CHANNEL_ID, text=build_closed_post_text("🔄", "..."))
            with lock:
                data["closed_message_id"] = m.message_id
                save_data()
            start_closed_anim(context.job_queue)
        except Exception:
            pass

        stop_live_countdown()
        try:
            query.edit_message_text("✅ Giveaway Closed Successfully! Now use /draw or AutoDraw will run if ON.")
        except Exception:
            pass

        if data.get("auto_draw", False):
            try:
                start_auto_draw(context)
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

    # Reset confirm/cancel
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

        stop_live_countdown()
        stop_draw_jobs()
        stop_closed_anim()
        stop_auto_draw_jobs()

        with lock:
            # delete current channel messages best-effort
            for mid_key in ["live_message_id", "closed_message_id"]:
                mid = data.get(mid_key)
                if mid:
                    try:
                        context.bot.delete_message(chat_id=CHANNEL_ID, message_id=mid)
                    except Exception:
                        pass

            keep_perma = dict(data.get("permanent_block", {}) or {})
            keep_verify = list(data.get("verify_targets", []) or [])
            keep_history = dict(data.get("history", {}) or {})
            keep_seq = int(data.get("giveaway_seq", 0))
            keep_auto = bool(data.get("auto_draw", False))

            data.clear()
            data.update(fresh_default_data())
            data["permanent_block"] = keep_perma
            data["verify_targets"] = keep_verify
            data["history"] = keep_history
            data["giveaway_seq"] = keep_seq
            data["auto_draw"] = keep_auto
            save_data()

        try:
            query.edit_message_text("✅ RESET COMPLETED!\nStart again with /newgiveaway")
        except Exception:
            pass
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

        if not verify_user_join(context.bot, int(uid)):
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

        if data.get("old_winner_mode") == "block":
            if uid in (data.get("old_winners", {}) or {}):
                try:
                    query.answer(popup_old_winner_blocked(), show_alert=True)
                except Exception:
                    pass
                return

        with lock:
            first_uid = data.get("first_winner_id")

        # same first join popup always
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

        # update live post
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

        # respond popup
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

    # Claim prize (per giveaway)
    if qd.startswith("claim_prize"):
        parts = qd.split("|", 1)
        gid = parts[1].strip() if len(parts) == 2 else ""

        with lock:
            snap = (data.get("history", {}) or {}).get(gid)

        if not snap:
            try:
                query.answer("This giveaway post is outdated or not found.", show_alert=True)
            except Exception:
                pass
            return

        winners = snap.get("winners", {}) or {}

        if uid not in winners:
            try:
                query.answer(popup_claim_not_winner(), show_alert=True)
            except Exception:
                pass
            return

        exp_ts = snap.get("claim_expires_ts")
        if exp_ts:
            try:
                if now_ts() > float(exp_ts):
                    query.answer(popup_prize_expired(), show_alert=True)
                    return
            except Exception:
                pass

        delivered = snap.get("delivered", {}) or {}
        uname = winners.get(uid, {}).get("username", "") or "@username"

        if delivered.get(uid):
            try:
                query.answer(
                    "📦 PRIZE ALREADY DELIVERED\n"
                    "Your prize has already been\n"
                    "successfully delivered ✅\n"
                    f"👤 {uname}\n"
                    f"🆔 {uid}\n"
                    "If you face any issue,\n"
                    f"contact admin 👉 {snap.get('admin_contact', ADMIN_CONTACT)}",
                    show_alert=True
                )
            except Exception:
                pass
            return

        try:
            query.answer(
                "🌟Congratulations✨\n"
                "You’ve won this giveaway.\n"
                f"🎯 Giveaway: {snap.get('title','')}\n"
                f"🎁 Prize: {snap.get('prize','')}\n"
                f"👤 {uname} | 🆔 {uid}\n"
                "📩 Please contact admin to claim:\n"
                f"👉 {snap.get('admin_contact', ADMIN_CONTACT)}",
                show_alert=True
            )
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

    # verify
    dp.add_handler(CommandHandler("addverifylink", cmd_addverifylink))
    dp.add_handler(CommandHandler("removeverifylink", cmd_removeverifylink))

    # giveaway
    dp.add_handler(CommandHandler("newgiveaway", cmd_newgiveaway))
    dp.add_handler(CommandHandler("participants", cmd_participants))
    dp.add_handler(CommandHandler("endgiveaway", cmd_endgiveaway))
    dp.add_handler(CommandHandler("draw", cmd_draw))

    # autodraw
    dp.add_handler(CommandHandler("Autodraw", cmd_autodraw))

    # prize delivery
    dp.add_handler(CommandHandler("prizeDelivered", cmd_prize_delivered))

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

    if data.get("closed") and data.get("closed_message_id"):
        start_closed_anim(updater.job_queue)

    print("Bot is running (PTB 13, GSM compatible, non-async) ...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
