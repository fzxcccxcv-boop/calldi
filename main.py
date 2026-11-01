import os
import re
import json
import wave
import time
import base64
import random
import shutil
import asyncio
import subprocess
import traceback
from datetime import datetime
from typing import Any
from collections import defaultdict

from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ChatPrivileges
)
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    PasswordHashInvalid, UserAlreadyParticipant, FloodWait
)
from pyrogram.raw.functions.phone import CreateGroupCall

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality
from pytgcalls.exceptions import NoActiveGroupCall

try:
    from pytgcalls.types import StreamAudioEnded as StreamEndEvent
except ImportError:
    try:
        from pytgcalls.types import StreamEnded as StreamEndEvent
    except ImportError:
        StreamEndEvent = None

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
API_ID = int(os.environ.get("API_ID", 35222361))
API_HASH = os.environ.get("API_HASH", "37e25f9cff15259a786c55da0b0af3f3")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8570478868:AAGRM_w35z1kFdZbWLksjcPpB8M1l6TX41s")
OWNER_ID = int(os.environ.get("OWNER_ID", 8797691602))
PORT = int(os.environ.get("PORT", 8080))

os.makedirs("audios", exist_ok=True)
os.makedirs("audios/processed", exist_ok=True)
os.makedirs("sessions", exist_ok=True)
DB_FILE = "lexii_db.json"
SILENT_WAV = os.path.abspath("audios/silent.wav")

if not os.path.exists(SILENT_WAV):
    with wave.open(SILENT_WAV, "w") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(48000)
        f.writeframes(b"\x00\x00\x00\x00" * 48000 * 10)

# ==========================================
# 🎚️ FFMPEG AUDIO FILTERS
# ==========================================
AUDIO_FILTERS = {
    "-xtrem": "bass=g=18:f=110:w=0.6,treble=g=8:f=3500,volume=7.0,aecho=0.8:0.9:100:0.5",
    "-flow": "bass=g=12:f=100:w=0.5,volume=4.0,aecho=0.8:0.88:60:0.4",
    "-adv": "bass=g=14:f=120:w=0.6,treble=g=10:f=3000,volume=5.5,aecho=0.8:0.9:120:0.6",
    "-dynamic": "acompressor=threshold=0.05:ratio=9:attack=5:release=50,bass=g=16:f=110:w=0.6,volume=6.0,aecho=0.8:0.88:40:0.4",
    "vl_max": "bass=g=22:f=110:w=0.7,treble=g=10:f=3500,volume=10.0,aecho=0.8:0.9:100:0.5"
}

def build_boosted_wav(input_path: str, mode_key: str) -> str:
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Audio file not found: {input_path}")
    safe_mode = mode_key.replace("-", "")
    out_wav = os.path.abspath(f"audios/processed/ready_{safe_mode}.wav")
    af_chain = AUDIO_FILTERS.get(mode_key, AUDIO_FILTERS["-xtrem"])
    if not shutil.which("ffmpeg"):
        print("⚠️ FFmpeg missing!")
        return os.path.abspath(input_path)
    cmd = ["ffmpeg", "-y", "-i", os.path.abspath(input_path), "-af", af_chain,
           "-ac", "2", "-ar", "48000", "-acodec", "pcm_s16le", out_wav]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1024:
        return out_wav
    fallback_cmd = ["ffmpeg", "-y", "-i", os.path.abspath(input_path),
                    "-af", "volume=5.0", "-ac", "2", "-ar", "48000",
                    "-acodec", "pcm_s16le", out_wav]
    subprocess.run(fallback_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return out_wav if os.path.exists(out_wav) else os.path.abspath(input_path)

# ==========================================
# 🗄️ DATABASE
# ==========================================
def load_db():
    if not os.path.exists(DB_FILE):
        default = {
            "admins": [OWNER_ID],
            "sudousers": [],
            "public_admin_mode": False,
            "whitelist_only_mode": False,
            "whitelist": [],
            "sessions": [],
            "rr_texts": [
                "🔥 Lexii Swarm Target Locked!",
                "⚡ Keep running, you can't hide!",
                "💥 Destroyed by Lexii Multi-Userbot!"
            ],
            "audios": {"default": None, "-xtrem": None, "-flow": None, "-adv": None, "-dynamic": None},
            "banned_users": [],
            "banned_groups": [],
            "host_history": [],
            "user_stats": {},
            "user_notes": {},
            "group_stats": {},
            "active_calls": {},
            "audit_log": [],
            "priority_bots": [],
            "command_usage": {},
            "mode_usage": {},
            "started_at": time.time(),
            "settings": {
                "maintenance": False,
                "maintenance_message": "🔧 Lexii is under scheduled maintenance. Please try again later.",
                "max_bots_per_user": 0,
                "max_sessions_total": 0,
                "auto_cleanup_days": 0,
                "force_sub_channel": None,
                "log_channel": None,
                "rr_delay": 0.6,
                "anti_flood_delay": 1.0,
            }
        }
        save_db(default)
        return default

    with open(DB_FILE, "r") as f:
        data = json.load(f)

    changed = False
    defaults = {
        "sudousers": [], "banned_users": [], "banned_groups": [],
        "whitelist": [], "host_history": [], "user_stats": {},
        "user_notes": {}, "group_stats": {}, "active_calls": {},
        "audit_log": [], "priority_bots": [], "command_usage": {},
        "mode_usage": {}, "started_at": time.time(),
    }
    for k, v in defaults.items():
        if k not in data:
            data[k] = v
            changed = True

    if "settings" not in data:
        data["settings"] = {}
        changed = True
    for k, v in {
        "maintenance": False,
        "maintenance_message": "🔧 Lexii is under maintenance.",
        "max_bots_per_user": 0,
        "max_sessions_total": 0,
        "auto_cleanup_days": 0,
        "force_sub_channel": None,
        "log_channel": None,
        "rr_delay": 0.6,
        "anti_flood_delay": 1.0,
    }.items():
        if k not in data["settings"]:
            data["settings"][k] = v
            changed = True

    if "whitelist_only_mode" not in data:
        data["whitelist_only_mode"] = False
        changed = True

    if changed:
        save_db(data)
    return data

def save_db(data):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"⚠️ DB save error: {e}")

db = load_db()

USER_STATES = {}
ACTIVE_USERBOTS = []
ACTIVE_LOOPS = {}
ACTIVE_RR_TASKS = {}
PROCESSED_MSGS = set()
USER_COOLDOWNS = defaultdict(dict)

# ==========================================
# 🔐 AUTH HELPERS
# ==========================================
def is_banned(uid: int) -> bool:
    return uid in db.get("banned_users", [])

def is_sudo(uid: int) -> bool:
    return uid == OWNER_ID or uid in db.get("sudousers", [])

def is_authorized(uid: int) -> bool:
    if is_banned(uid):
        return False
    if uid == OWNER_ID:
        return True
    if uid in db.get("sudousers", []):
        return True
    if db.get("whitelist_only_mode", False):
        return uid in db.get("whitelist", [])
    if db.get("public_admin_mode", False):
        return True
    return uid in db.get("admins", [])

def is_maintenance_blocked(uid: int) -> bool:
    if uid == OWNER_ID or uid in db.get("sudousers", []):
        return False
    return db.get("settings", {}).get("maintenance", False)

def check_cooldown(uid: int, cmd: str, cooldown: float = 2.0) -> bool:
    now = time.time()
    last = USER_COOLDOWNS[uid].get(cmd, 0)
    if now - last < cooldown:
        return False
    USER_COOLDOWNS[uid][cmd] = now
    return True

def audit(user_id: int, action: str, detail: str = ""):
    db.setdefault("audit_log", []).append({
        "user": user_id,
        "action": action,
        "detail": detail,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    if len(db["audit_log"]) > 500:
        db["audit_log"] = db["audit_log"][-500:]
    save_db(db)

async def log_event(text: str):
    ch = db.get("settings", {}).get("log_channel")
    if not ch:
        return
    try:
        await bot.send_message(ch, text)
    except Exception:
        pass

async def check_force_sub(user_id: int) -> bool:
    ch = db.get("settings", {}).get("force_sub_channel")
    if not ch:
        return True
    if is_sudo(user_id):
        return True
    try:
        m = await bot.get_chat_member(ch, user_id)
        return m.status not in ["left", "kicked"]
    except Exception:
        return True

# ==========================================
# 🔗 SWARM JOINER
# ==========================================
def normalize_chat_target(raw: str) -> str:
    target = raw.strip()
    if "joinchat/" in target or "t.me/+" in target:
        if not target.startswith("http"):
            target = "https://" + target.lstrip("/")
        return target
    m = re.match(r"^(?:https?://)?t\.me/([a-zA-Z0-9_]+)/?$", target)
    if m:
        return m.group(1)
    return target.lstrip("@")

async def _single_ub_join(ub_entry: dict, target: str) -> tuple[bool, str]:
    client: Client = ub_entry["client"]
    try:
        chat = await client.join_chat(target)
        return True, f"✅ `{ub_entry['name']}` joined (`{chat.title}`)"
    except UserAlreadyParticipant:
        return True, f"☑️ `{ub_entry['name']}` already in group"
    except FloodWait as fw:
        await asyncio.sleep(min(fw.value, 15))
        try:
            await client.join_chat(target)
            return True, f"✅ `{ub_entry['name']}` joined (after FloodWait)"
        except Exception as e:
            return False, f"❌ `{ub_entry['name']}`: `{type(e).__name__}`"
    except Exception as e:
        return False, f"❌ `{ub_entry['name']}`: `{type(e).__name__}`"

async def swarm_join_group(raw_link: str) -> tuple[int, list[str]]:
    if not ACTIVE_USERBOTS:
        return 0, ["⚠️ No hosted userbots active!"]
    target = normalize_chat_target(raw_link)
    results = await asyncio.gather(
        *[_single_ub_join(ub, target) for ub in ACTIVE_USERBOTS],
        return_exceptions=True
    )
    ok_count = 0
    details = []
    for r in results:
        if isinstance(r, tuple):
            ok, msg = r
            if ok: ok_count += 1
            details.append(msg)
        else:
            details.append(f"❌ Error: `{r}`")
    return ok_count, details

# ==========================================
# 🎯 RR ENGINE
# ==========================================
async def _rr_swipe_loop(chat_id: int, target_id: int, target_name: str, reply_msg_id: int | None):
    mention = f"[{target_name}](tg://user?id={target_id})" if target_id else target_name
    delay = float(db.get("settings", {}).get("rr_delay", 0.6))
    anti_flood = float(db.get("settings", {}).get("anti_flood_delay", 1.0))
    while ACTIVE_RR_TASKS.get(chat_id, {}).get("active", False):
        texts = db.get("rr_texts", []) or ["🔥 Target locked!"]
        for ub in list(ACTIVE_USERBOTS):
            if not ACTIVE_RR_TASKS.get(chat_id, {}).get("active", False):
                break
            c: Client = ub["client"]
            chosen = random.choice(texts)
            try:
                if reply_msg_id:
                    await c.send_message(chat_id, f"{mention} {chosen}", reply_to_message_id=reply_msg_id)
                else:
                    await c.send_message(chat_id, f"{mention} {chosen}")
            except FloodWait as fw:
                await asyncio.sleep(min(fw.value, 10))
            except Exception:
                try:
                    await c.send_message(chat_id, f"{mention} {chosen}")
                except Exception:
                    pass
            await asyncio.sleep(delay)
        await asyncio.sleep(anti_flood * 0.4)

# ==========================================
# 🤖 MAIN BOT
# ==========================================
bot = Client("lexii_controller_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def get_main_menu():
    pub = "🟢 ON" if db.get("public_admin_mode") else "🔴 OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload Audio / VN", callback_data="menu_upload"),
         InlineKeyboardButton("🚀 Host Userbot", callback_data="menu_host")],
        [InlineKeyboardButton("🔗 Swarm Join Group", callback_data="menu_join"),
         InlineKeyboardButton("👮 Admin Panel", callback_data="menu_admin")],
        [InlineKeyboardButton("👑 Owner Panel", callback_data="menu_owner"),
         InlineKeyboardButton(f"🌐 Everyone Admin: {pub}", callback_data="toggle_public_admin")],
        [InlineKeyboardButton("📊 System Stats", callback_data="public_stats"),
         InlineKeyboardButton("ℹ️ My Profile", callback_data="public_profile")]
    ])

def build_owner_text():
    s = db.get("user_stats", {})
    return (
        f"👑 **Lexii Owner Control Panel v3.0**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🐝 Active Userbots: `{len(ACTIVE_USERBOTS)}`\n"
        f"💾 Saved Sessions: `{len(db.get('sessions', []))}`\n"
        f"👥 Unique Hosts: `{len(s)}`\n"
        f"📈 Total Host Events: `{sum(v.get('hosted_count',0) for v in s.values())}`\n"
        f"🚫 Banned Users: `{len(db.get('banned_users', []))}`\n"
        f"🚫 Banned Groups: `{len(db.get('banned_groups', []))}`\n"
        f"📝 .rr Texts: `{len(db.get('rr_texts', []))}`\n"
        f"👮 Admins: `{len(db.get('admins', []))}` · 🌟 Sudo: `{len(db.get('sudousers', []))}`\n"
        f"📞 Active Calls: `{len(db.get('active_calls', {}))}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Uptime: `{format_uptime(time.time() - db.get('started_at', time.time()))}`"
    )

def build_owner_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics", callback_data="owner_stats"),
         InlineKeyboardButton("👥 Users", callback_data="owner_user_list")],
        [InlineKeyboardButton("🚫 Bans", callback_data="owner_bans"),
         InlineKeyboardButton("📜 Host History", callback_data="owner_host_history")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="owner_settings"),
         InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast")],
        [InlineKeyboardButton("💾 Export DB", callback_data="owner_export_db"),
         InlineKeyboardButton("📥 Import DB", callback_data="owner_import_db")],
        [InlineKeyboardButton("📞 Active Calls", callback_data="owner_active_calls"),
         InlineKeyboardButton("📋 Audit Log", callback_data="owner_audit_log")],
        [InlineKeyboardButton("🌟 Manage Sudo", callback_data="owner_sudo"),
         InlineKeyboardButton("👮 Manage Admins", callback_data="owner_admins")],
        [InlineKeyboardButton("🎵 Manage .rr Texts", callback_data="owner_rr_panel"),
         InlineKeyboardButton("🌐 Toggle Public Admin", callback_data="toggle_public_admin")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ])

def build_settings_kb():
    s = db.get("settings", {})
    maint = s.get("maintenance", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔧 Maintenance: {'🔴 ON' if maint else '🟢 OFF'}", callback_data="toggle_maintenance")],
        [InlineKeyboardButton(f"📊 Max Bots/User: {s.get('max_bots_per_user') or '∞'}", callback_data="owner_set_max_bots")],
        [InlineKeyboardButton(f"💾 Max Sessions: {s.get('max_sessions_total') or '∞'}", callback_data="owner_set_max_sessions")],
        [InlineKeyboardButton(f"🚪 Force-Sub: {s.get('force_sub_channel') or 'Off'}", callback_data="owner_set_force_sub")],
        [InlineKeyboardButton(f"📝 Log Channel: {s.get('log_channel') or 'Off'}", callback_data="owner_set_log_channel")],
        [InlineKeyboardButton(f"⏱ .rr Delay: {s.get('rr_delay', 0.6)}s", callback_data="owner_set_rr_delay")],
        [InlineKeyboardButton(f"🛡 Anti-Flood: {s.get('anti_flood_delay', 1.0)}s", callback_data="owner_set_anti_flood")],
        [InlineKeyboardButton(f"🌐 Whitelist Mode: {'🟢 ON' if db.get('whitelist_only_mode') else '🔴 OFF'}", callback_data="toggle_whitelist")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
    ])

def format_uptime(sec):
    d = int(sec // 86400); h = int((sec % 86400) // 3600); m = int((sec % 3600) // 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m"
    return f"{m}m"

# ==========================================
# 🚀 START COMMAND
# ==========================================
@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    USER_STATES.pop(message.from_user.id, None)

    if is_banned(message.from_user.id):
        return await message.reply_text("🚫 You are banned from using Lexii Swarm.")
    if not await check_force_sub(message.from_user.id):
        ch = db["settings"]["force_sub_channel"]
        return await message.reply_text(
            f"⚠️ **Join our channel first:**\n👉 {ch}\n\nThen send /start again."
        )
    if is_maintenance_blocked(message.from_user.id):
        return await message.reply_text(db["settings"].get("maintenance_message"))

    ffmpeg = "🟢 Installed" if shutil.which("ffmpeg") else "🔴 MISSING"
    await message.reply_text(
        f"🎙 **Welcome to Lexii Swarm v3.0**\n\n"
        f"🤖 **Active Userbots:** `{len(ACTIVE_USERBOTS)}`\n"
        f"🎚 **FFmpeg:** `{ffmpeg}`\n"
        f"⏱ **Uptime:** `{format_uptime(time.time() - db.get('started_at', time.time()))}`\n\n"
        f"Use the buttons below to control the swarm.",
        reply_markup=get_main_menu()
    )

@bot.on_message(filters.command("id") & filters.private)
async def id_cmd(client: Client, message: Message):
    text = f"👤 Your ID: `{message.from_user.id}`"
    if message.reply_to_message and message.reply_to_message.from_user:
        text += f"\n👥 Replied User ID: `{message.reply_to_message.from_user.id}`"
    await message.reply_text(text)

@bot.on_message(filters.command("stats") & filters.private)
async def stats_cmd(client: Client, message: Message):
    s = db.get("user_stats", {})
    await message.reply_text(
        f"📊 **System Statistics**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🐝 Active Userbots: `{len(ACTIVE_USERBOTS)}`\n"
        f"💾 Sessions: `{len(db.get('sessions', []))}`\n"
        f"👥 Unique Hosts: `{len(s)}`\n"
        f"📈 Total Hosts: `{sum(v.get('hosted_count',0) for v in s.values())}`\n"
        f"📞 Active Calls: `{len(db.get('active_calls', {}))}`\n"
        f"⏱ Uptime: `{format_uptime(time.time() - db.get('started_at', time.time()))}`",
        reply_markup=get_main_menu()
    )

@bot.on_message(filters.private & filters.regex(r"(?i)^join\s+(.+)"))
async def bot_pm_join_cmd(client: Client, message: Message):
    if is_banned(message.from_user.id):
        return await message.reply_text("🚫 Banned.")
    if not is_authorized(message.from_user.id):
        return await message.reply_text("❌ Not authorized.")
    if is_maintenance_blocked(message.from_user.id):
        return await message.reply_text(db["settings"].get("maintenance_message"))
    if not check_cooldown(message.from_user.id, "join", 3.0):
        return await message.reply_text("⏳ Please wait before running `join` again.")

    raw = message.matches[0].group(1).strip()
    status = await message.reply_text(f"🐝 Joining `{len(ACTIVE_USERBOTS)}` bot(s)...")
    ok, details = await swarm_join_group(raw)
    await status.edit_text(
        f"🔗 **Swarm Join Report ({ok}/{len(ACTIVE_USERBOTS)} Ready)**\n\n"
        + "\n".join(details) +
        f"\n\n💡 Type `cll` in the group to start the voice call.",
        reply_markup=get_main_menu()
    )

# ==========================================
# 📞 CALLBACK HANDLER
# ==========================================
@bot.on_callback_query()
async def callback_handler(client: Client, cb: CallbackQuery):
    uid = cb.from_user.id
    data = cb.data

    try:
        if is_banned(uid):
            return await cb.answer("🚫 You are banned.", show_alert=True)

        # ─── MAIN MENU ───
        if data == "back_main":
            USER_STATES.pop(uid, None)
            await cb.message.edit_text(
                f"🎙 **Lexii Swarm Controller v3.0**\n"
                f"🤖 Active Bots: `{len(ACTIVE_USERBOTS)}`",
                reply_markup=get_main_menu()
            )

        elif data == "public_stats":
            s = db.get("user_stats", {})
            await cb.message.edit_text(
                f"📊 **Public Stats**\n"
                f"🐝 Active Userbots: `{len(ACTIVE_USERBOTS)}`\n"
                f"👥 Total Hosts: `{len(s)}`\n"
                f"📞 Active Calls: `{len(db.get('active_calls', {}))}`\n"
                f"⏱ Uptime: `{format_uptime(time.time() - db.get('started_at', time.time()))}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
            )

        elif data == "public_profile":
            st = db.get("user_stats", {}).get(str(uid), {})
            await cb.message.edit_text(
                f"ℹ️ **Your Profile**\n"
                f"🆔 ID: `{uid}`\n"
                f"📈 Hosted Bots: `{st.get('hosted_count', 0)}`\n"
                f"👑 Admin: `{'Yes' if uid in db.get('admins', []) else 'No'}`\n"
                f"🌟 Sudo: `{'Yes' if uid in db.get('sudousers', []) else 'No'}`\n"
                f"🚫 Banned: `{'Yes' if is_banned(uid) else 'No'}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
            )

        # ─── JOIN MENU ───
        elif data == "menu_join":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "awaiting_join_link"}
            await cb.message.edit_text(
                "🔗 **Swarm Auto-Join Group**\n\n"
                "Send the **Invite Link** (`https://t.me/+...`) or **Username** (`@groupname`).\n"
                "All active userbots will join together.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]])
            )

        # ─── UPLOAD MENU ───
        elif data == "menu_upload":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔥 -xtrem (Earrape+Bass)", callback_data="up_-xtrem"),
                 InlineKeyboardButton("🌊 -flow (Deep Bass)", callback_data="up_-flow")],
                [InlineKeyboardButton("⚡ -adv (High Reverb)", callback_data="up_-adv"),
                 InlineKeyboardButton("🌀 -dynamic (Compressor)", callback_data="up_-dynamic")],
                [InlineKeyboardButton("🎵 Set for ALL Modes (Master)", callback_data="up_default")],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ])
            await cb.message.edit_text("📤 **Upload Audio / VN**\n\nSelect a mode:", reply_markup=kb)

        elif data.startswith("up_"):
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            mode = data.replace("up_", "")
            USER_STATES[uid] = {"action": "upload_audio", "mode": mode}
            await cb.message.edit_text(
                f"🎙 **Send your Voice Note / Audio file** for `{mode}`.\n*(/start to cancel)*"
            )

        # ─── HOST MENU ───
        elif data == "menu_host":
            if not is_authorized(uid):
                return await cb.answer("❌ Not authorized.", show_alert=True)
            if is_maintenance_blocked(uid):
                return await cb.answer("🔧 Maintenance mode.", show_alert=True)
            max_bots = db.get("settings", {}).get("max_bots_per_user", 0)
            if max_bots > 0:
                cur = db.get("user_stats", {}).get(str(uid), {}).get("hosted_count", 0)
                if cur >= max_bots:
                    return await cb.answer(f"❌ Limit reached ({max_bots} bots max).", show_alert=True)
            USER_STATES[uid] = {"action": "awaiting_phone"}
            await cb.message.edit_text(
                "📱 **Userbot Hoster**\n\nStep 1/3: Send phone number with country code.\nExample: `+919876543210`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]])
            )

        # ─── ADMIN PANEL ───
        elif data == "menu_admin":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            admins = "\n".join([f"• `{a}`" for a in db["admins"]]) or "None"
            ub_names = ", ".join([f"`{u['name']}`" for u in ACTIVE_USERBOTS]) or "None"
            await cb.message.edit_text(
                f"👮 **Admin Panel**\n\n"
                f"🤖 **Active Bots ({len(ACTIVE_USERBOTS)}):** {ub_names}\n"
                f"👥 **Admins:**\n{admins}\n"
                f"📝 **.rr Texts:** `{len(db.get('rr_texts', []))}`\n\n"
                f"💡 **Commands:**\n"
                f"• `hey lexii` / `menu`\n"
                f"• `join <link>` · `cll`\n"
                f"• `-xtrem` `-flow` `-adv` `-dynamic`\n"
                f"• `pe` `rs` `st` `vl`\n"
                f"• `.rr` / `.srr`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
            )

        # ─── OWNER PANEL ───
        elif data == "menu_owner":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            await cb.message.edit_text(build_owner_text(), reply_markup=build_owner_kb())

        elif data == "owner_stats":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            s = db.get("user_stats", {})
            total = sum(v.get("hosted_count", 0) for v in s.values())
            modes = db.get("mode_usage", {})
            mode_str = " · ".join(f"`{k}`:{v}" for k, v in modes.items()) or "No data"
            await cb.message.edit_text(
                f"📊 **Detailed Statistics**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🐝 Active Userbots: `{len(ACTIVE_USERBOTS)}`\n"
                f"💾 Sessions: `{len(db.get('sessions', []))}`\n"
                f"👥 Unique Hosts: `{len(s)}`\n"
                f"📈 Total Host Events: `{total}`\n"
                f"🚫 Banned Users: `{len(db.get('banned_users', []))}`\n"
                f"🚫 Banned Groups: `{len(db.get('banned_groups', []))}`\n"
                f"📞 Active Calls: `{len(db.get('active_calls', {}))}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎚 **Mode Usage:** {mode_str}\n"
                f"⏱ **Uptime:** `{format_uptime(time.time() - db.get('started_at', time.time()))}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]])
            )

        elif data == "owner_user_list":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            s = db.get("user_stats", {})
            if not s:
                return await cb.answer("No users yet.", show_alert=True)
            sorted_u = sorted(s.items(), key=lambda x: x[1].get("hosted_count", 0), reverse=True)
            lines = []
            for u, info in sorted_u[:15]:
                b = "🚫" if int(u) in db.get("banned_users", []) else "✅"
                un = f"@{info['username']}" if info.get("username") else ""
                lines.append(f"{b} `{u}` **{info.get('first_name','?')}** {un}\n   📈 `{info.get('hosted_count',0)}` host(s)")
            await cb.message.edit_text(
                f"👥 **Users** (Top {min(15,len(sorted_u))}/{len(sorted_u)})\n\n" + "\n\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Search User", callback_data="owner_search_user")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
                ])
            )

        elif data == "owner_search_user":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "search_user"}
            await cb.message.edit_text(
                "🔍 **Search User**\n\nSend the User ID or username (without @).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="menu_owner")]])
            )

        elif data == "owner_bans":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            await cb.message.edit_text(
                f"🚫 **Ban Management**\n"
                f"Users: `{len(db.get('banned_users', []))}` · Groups: `{len(db.get('banned_groups', []))}`",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚫 Ban User", callback_data="owner_ban_user"),
                     InlineKeyboardButton("✅ Unban User", callback_data="owner_unban_user")],
                    [InlineKeyboardButton("🚫 Ban Group", callback_data="owner_ban_group"),
                     InlineKeyboardButton("✅ Unban Group", callback_data="owner_unban_group")],
                    [InlineKeyboardButton("📋 View Banned Users", callback_data="owner_banned_list"),
                     InlineKeyboardButton("📋 View Banned Groups", callback_data="owner_banned_groups")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
                ])
            )

        elif data == "owner_ban_user":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "ban_user"}
            await cb.message.edit_text("🚫 Send User ID to ban:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_bans")]]))

        elif data == "owner_unban_user":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "unban_user"}
            await cb.message.edit_text("✅ Send User ID to unban:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_bans")]]))

        elif data == "owner_ban_group":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "ban_group"}
            await cb.message.edit_text("🚫 Send Group ID to ban:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_bans")]]))

        elif data == "owner_unban_group":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "unban_group"}
            await cb.message.edit_text("✅ Send Group ID to unban:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_bans")]]))

        elif data == "owner_banned_list":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            banned = db.get("banned_users", [])
            if not banned:
                return await cb.answer("Banned list empty!", show_alert=True)
            stats = db.get("user_stats", {})
            lines = []
            for b in banned[:20]:
                info = stats.get(str(b), {})
                lines.append(f"🚫 `{b}` **{info.get('first_name','?')}**")
            await cb.message.edit_text(
                f"📋 **Banned Users** ({len(banned)})\n\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_bans")]])
            )

        elif data == "owner_banned_groups":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            banned = db.get("banned_groups", [])
            if not banned:
                return await cb.answer("No banned groups!", show_alert=True)
            await cb.message.edit_text(
                f"📋 **Banned Groups** ({len(banned)})\n\n" + "\n".join(f"• `{g}`" for g in banned[:30]),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="owner_bans")]])
            )

        elif data == "owner_host_history":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            h = db.get("host_history", [])
            if not h:
                return await cb.answer("No history yet!", show_alert=True)
            lines = []
            for e in list(reversed(h))[:12]:
                lines.append(
                    f"📱 **{e.get('userbot_name','?')}** `{e.get('userbot_id','?')}`\n"
                    f"   👤 Host: `{e.get('user_id','?')}` · 📞 `{e.get('phone','N/A')}`\n"
                    f"   🕒 {e.get('datetime','—')}"
                )
            await cb.message.edit_text(
                f"📜 **Host History** (latest {min(12,len(h))}/{len(h)})\n\n" + "\n\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]])
            )

        # ─── SETTINGS ───
        elif data == "owner_settings":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            await cb.message.edit_text(
                "⚙️ **Settings & Management**",
                reply_markup=build_settings_kb()
            )

        elif data == "toggle_maintenance":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            s = db.setdefault("settings", {})
            s["maintenance"] = not s.get("maintenance", False)
            save_db(db)
            await cb.answer(f"Maintenance: {'ON' if s['maintenance'] else 'OFF'}", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=build_settings_kb())

        elif data == "owner_set_max_bots":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "set_max_bots"}
            await cb.message.edit_text("📊 Send max bots per user (0 = unlimited):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]]))

        elif data == "owner_set_max_sessions":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "set_max_sessions"}
            await cb.message.edit_text("💾 Send max total sessions (0 = unlimited):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]]))

        elif data == "owner_set_force_sub":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "set_force_sub"}
            await cb.message.edit_text("🚪 Send channel username (e.g. `@mychannel`) or `off` to disable:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]]))

        elif data == "owner_set_log_channel":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "set_log_channel"}
            await cb.message.edit_text("📝 Send log channel ID or username, or `off`:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]]))

        elif data == "owner_set_rr_delay":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "set_rr_delay"}
            await cb.message.edit_text("⏱ Send .rr delay in seconds (e.g. `0.6`):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]]))

        elif data == "owner_set_anti_flood":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "set_anti_flood"}
            await cb.message.edit_text("🛡 Send anti-flood delay in seconds (e.g. `1.0`):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]]))

        elif data == "toggle_whitelist":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            db["whitelist_only_mode"] = not db.get("whitelist_only_mode", False)
            save_db(db)
            await cb.answer(f"Whitelist Mode: {'ON' if db['whitelist_only_mode'] else 'OFF'}", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=build_settings_kb())

        # ─── BROADCAST ───
        elif data == "owner_broadcast":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "broadcast"}
            await cb.message.edit_text(
                "📢 **Broadcast**\n\nSend the message to broadcast to all hosts.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]])
            )

        # ─── EXPORT / IMPORT DB ───
        elif data == "owner_export_db":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            try:
                content = json.dumps(db, indent=4)
                path = f"sessions/lexii_db_backup_{int(time.time())}.json"
                with open(path, "w") as f:
                    f.write(content)
                await cb.message.reply_document(
                    path,
                    caption=f"💾 **DB Backup**\nUptime: `{format_uptime(time.time() - db.get('started_at', time.time()))}`"
                )
                os.remove(path)
                await cb.answer("✅ DB exported!")
            except Exception as e:
                await cb.answer(f"❌ {e}", show_alert=True)

        elif data == "owner_import_db":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            USER_STATES[uid] = {"action": "import_db"}
            await cb.message.edit_text(
                "📥 **Import DB**\n\nSend the `.json` backup file.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_settings")]])
            )

        # ─── ACTIVE CALLS ───
        elif data == "owner_active_calls":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            calls = db.get("active_calls", {})
            if not calls:
                return await cb.answer("No active calls.", show_alert=True)
            lines = []
            for cid, info in list(calls.items())[:15]:
                started = info.get("started", time.time())
                dur = format_uptime(time.time() - started)
                lines.append(
                    f"📞 `{cid}` — mode `{info.get('mode','?')}` · {info.get('bots',0)} bots · {dur}"
                )
            await cb.message.edit_text(
                f"📞 **Active Calls** ({len(calls)})\n\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚪 Force Leave All", callback_data="owner_force_leave_calls")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
                ])
            )

        elif data == "owner_force_leave_calls":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            await cb.answer("Force leaving all calls...", show_alert=True)
            for cid in list(db.get("active_calls", {}).keys()):
                await swarm_control_action(cid, "st")
            db["active_calls"] = {}
            save_db(db)
            await cb.message.edit_text("✅ All userbots left all calls.",
                reply_markup=build_owner_kb())

        # ─── AUDIT LOG ───
        elif data == "owner_audit_log":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            log = db.get("audit_log", [])
            if not log:
                return await cb.answer("No audit entries.", show_alert=True)
            lines = []
            for e in list(reversed(log))[:20]:
                lines.append(f"`{e['time']}` **{e['action']}** — user `{e['user']}`" +
                             (f" · {e['detail']}" if e.get("detail") else ""))
            await cb.message.edit_text(
                f"📋 **Audit Log** (latest 20)\n\n" + "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]])
            )

        # ─── SUDO MANAGEMENT ───
        elif data == "owner_sudo":
            if not is_sudo(uid):
                return await cb.answer("❌ Sudo only!", show_alert=True)
            sudos = "\n".join(f"• `{s}`" for s in db.get("sudousers", [])) or "None"
            await cb.message.edit_text(
                f"🌟 **Sudo Users**\n\n{sudos}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Sudo", callback_data="owner_add_sudo"),
                     InlineKeyboardButton("➖ Remove Sudo", callback_data="owner_rem_sudo")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
                ])
            )

        elif data == "owner_add_sudo":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            USER_STATES[uid] = {"action": "add_sudo"}
            await cb.message.edit_text("🌟 Send User ID to add as Sudo:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_sudo")]]))

        elif data == "owner_rem_sudo":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            USER_STATES[uid] = {"action": "rem_sudo"}
            await cb.message.edit_text("➖ Send User ID to remove from Sudo:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_sudo")]]))

        # ─── ADMIN MANAGEMENT ───
        elif data == "owner_admins":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            admins = "\n".join(f"• `{a}`" for a in db["admins"]) or "None"
            await cb.message.edit_text(
                f"👮 **Admins**\n\n{admins}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Admin", callback_data="owner_add_admin"),
                     InlineKeyboardButton("➖ Remove Admin", callback_data="owner_rem_admin")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
                ])
            )

        elif data == "owner_add_admin":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            USER_STATES[uid] = {"action": "add_admin"}
            await cb.message.edit_text("➕ Send User ID to add as Admin:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_admins")]]))

        elif data == "owner_rem_admin":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            USER_STATES[uid] = {"action": "rem_admin"}
            await cb.message.edit_text("➖ Send User ID to remove from Admins:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_admins")]]))

        # ─── RR TEXTS ───
        elif data == "owner_rr_panel":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            rr = db.get("rr_texts", [])
            preview = "\n".join(f"`{i+1}.` {t}" for i, t in enumerate(rr[-10:])) or "None"
            await cb.message.edit_text(
                f"📝 **.rr Texts** (`{len(rr)}` saved)\n\n{preview}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Add Texts", callback_data="owner_add_rr_text"),
                     InlineKeyboardButton("🗑 Clear All", callback_data="owner_clear_rr_text")],
                    [InlineKeyboardButton("🔙 Back", callback_data="menu_owner")]
                ])
            )

        elif data == "owner_add_rr_text":
            if not is_authorized(uid):
                return await cb.answer("❌ Admins only!", show_alert=True)
            USER_STATES[uid] = {"action": "add_rr_text"}
            await cb.message.edit_text(
                "📝 Send the `.rr` text (multiple lines = multiple texts).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="owner_rr_panel")]])
            )

        elif data == "owner_clear_rr_text":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            db["rr_texts"] = []
            save_db(db)
            await cb.answer("✅ .rr texts cleared!", show_alert=True)
            await cb.message.edit_text(build_owner_text(), reply_markup=build_owner_kb())

        # ─── TOGGLE PUBLIC ADMIN ───
        elif data == "toggle_public_admin":
            if uid != OWNER_ID:
                return await cb.answer("❌ Owner only!", show_alert=True)
            db["public_admin_mode"] = not db.get("public_admin_mode", False)
            save_db(db)
            status = "ENABLED" if db["public_admin_mode"] else "DISABLED"
            await cb.answer(f"Public Admin Mode: {status}", show_alert=True)
            await cb.message.edit_reply_markup(reply_markup=get_main_menu())

    except Exception as e:
        print(f"⚠️ Callback error: {e}")
        try:
            await cb.answer(f"❌ Error: {type(e).__name__}", show_alert=True)
        except Exception:
            pass

# ==========================================
# 📥 INPUT HANDLER
# ==========================================
@bot.on_message(filters.private & ~filters.command("start") & ~filters.command("id")
                & ~filters.command("stats") & ~filters.regex(r"(?i)^join\s+(.+)"))
async def handle_private_inputs(client: Client, message: Message):
    uid = message.from_user.id
    if uid not in USER_STATES:
        return
    if is_banned(uid):
        USER_STATES.pop(uid, None)
        return await message.reply_text("🚫 Banned.")
    if is_maintenance_blocked(uid):
        USER_STATES.pop(uid, None)
        return await message.reply_text(db["settings"].get("maintenance_message"))

    state = USER_STATES[uid]
    action = state.get("action")

    # ─── JOIN LINK ───
    if action == "awaiting_join_link":
        if not message.text:
            return await message.reply_text("❌ Send a valid link.")
        raw = message.text.strip()
        USER_STATES.pop(uid, None)
        status = await message.reply_text(f"🐝 Joining `{len(ACTIVE_USERBOTS)}` bot(s)...")
        ok, details = await swarm_join_group(raw)
        await status.edit_text(
            f"🔗 **Report ({ok}/{len(ACTIVE_USERBOTS)} ready)**\n\n" + "\n".join(details),
            reply_markup=get_main_menu()
        )

    # ─── ADD RR TEXT ───
    elif action == "add_rr_text":
        if not message.text:
            return await message.reply_text("❌ Invalid text.")
        lines = [l.strip() for l in message.text.splitlines() if l.strip()]
        if not lines:
            return await message.reply_text("❌ Empty.")
        db.setdefault("rr_texts", []).extend(lines)
        save_db(db)
        USER_STATES.pop(uid, None)
        audit(uid, "rr_add", f"{len(lines)} lines")
        await message.reply_text(
            f"✅ Added `{len(lines)}` text(s). Total: `{len(db['rr_texts'])}`",
            reply_markup=get_main_menu()
        )

    # ─── UPLOAD AUDIO ───
    elif action == "upload_audio":
        media = message.audio or message.voice or message.document
        if not media:
            return await message.reply_text("❌ Send a Voice Note or Audio file!")
        mode = state["mode"]
        status = await message.reply_text("⏳ Downloading & processing...")
        ext = ".ogg" if message.voice else ".mp3"
        raw = os.path.abspath(f"audios/raw_{mode}{ext}")
        await message.download(file_name=raw)
        loop = asyncio.get_running_loop()
        try:
            if mode == "default":
                for k in ["-xtrem", "-flow", "-adv", "-dynamic"]:
                    wav = await loop.run_in_executor(None, build_boosted_wav, raw, k)
                    db["audios"][k] = wav
                db["audios"]["default"] = db["audios"]["-xtrem"]
            else:
                wav = await loop.run_in_executor(None, build_boosted_wav, raw, mode)
                db["audios"][mode] = wav
                if not db["audios"]["default"]:
                    db["audios"]["default"] = wav
            save_db(db)
            USER_STATES.pop(uid, None)
            audit(uid, "audio_upload", mode)
            await status.edit_text(f"✅ Audio uploaded for `{mode}`!",
                                    reply_markup=get_main_menu())
        except Exception as e:
            await status.edit_text(f"❌ Error: `{e}`", reply_markup=get_main_menu())

    # ─── ADD ADMIN ───
    elif action == "add_admin":
        try:
            t = int(message.text.strip())
            if t not in db["admins"]:
                db["admins"].append(t)
                save_db(db)
                audit(uid, "admin_add", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ `{t}` added as Admin!", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    elif action == "rem_admin":
        try:
            t = int(message.text.strip())
            if t in db["admins"] and t != OWNER_ID:
                db["admins"].remove(t)
                save_db(db)
                audit(uid, "admin_rem", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ `{t}` removed.", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    # ─── SUDO ───
    elif action == "add_sudo":
        try:
            t = int(message.text.strip())
            if t not in db["sudousers"]:
                db["sudousers"].append(t)
                save_db(db)
                audit(uid, "sudo_add", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ `{t}` added as Sudo!", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    elif action == "rem_sudo":
        try:
            t = int(message.text.strip())
            if t in db["sudousers"]:
                db["sudousers"].remove(t)
                save_db(db)
                audit(uid, "sudo_rem", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ `{t}` removed from Sudo.", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    # ─── BAN / UNBAN USER ───
    elif action == "ban_user":
        try:
            t = int(message.text.strip())
            if t == OWNER_ID:
                return await message.reply_text("❌ Cannot ban the Owner.")
            if t not in db["banned_users"]:
                db["banned_users"].append(t)
                save_db(db)
                audit(uid, "ban_user", str(t))
            info = db.get("user_stats", {}).get(str(t), {})
            bot_ids = set(info.get("userbot_ids", []))
            stopped = 0
            for ub in list(ACTIVE_USERBOTS):
                if ub["id"] in bot_ids:
                    try: await ub["client"].stop()
                    except Exception: pass
                    try: await ub["call"].stop()
                    except Exception: pass
                    try: ACTIVE_USERBOTS.remove(ub)
                    except ValueError: pass
                    stopped += 1
            USER_STATES.pop(uid, None)
            msg = f"✅ User `{t}` banned."
            if stopped: msg += f"\n🛑 Stopped `{stopped}` bot(s)."
            await message.reply_text(msg, reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    elif action == "unban_user":
        try:
            t = int(message.text.strip())
            if t in db["banned_users"]:
                db["banned_users"].remove(t)
                save_db(db)
                audit(uid, "unban_user", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ `{t}` unbanned.", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    # ─── BAN / UNBAN GROUP ───
    elif action == "ban_group":
        try:
            t = int(message.text.strip())
            if t not in db["banned_groups"]:
                db["banned_groups"].append(t)
                save_db(db)
                audit(uid, "ban_group", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Group `{t}` banned.", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid Group ID.")

    elif action == "unban_group":
        try:
            t = int(message.text.strip())
            if t in db["banned_groups"]:
                db["banned_groups"].remove(t)
                save_db(db)
                audit(uid, "unban_group", str(t))
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Group `{t}` unbanned.", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid ID.")

    # ─── SETTINGS ───
    elif action == "set_max_bots":
        try:
            n = int(message.text.strip())
            db["settings"]["max_bots_per_user"] = max(0, n)
            save_db(db)
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Max bots/user: `{n or '∞'}`", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid number.")

    elif action == "set_max_sessions":
        try:
            n = int(message.text.strip())
            db["settings"]["max_sessions_total"] = max(0, n)
            save_db(db)
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Max sessions: `{n or '∞'}`", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid number.")

    elif action == "set_force_sub":
        try:
            v = message.text.strip()
            db["settings"]["force_sub_channel"] = None if v.lower() == "off" else v
            save_db(db)
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Force-sub: `{v}`", reply_markup=get_main_menu())
        except Exception as e:
            await message.reply_text(f"❌ {e}")

    elif action == "set_log_channel":
        try:
            v = message.text.strip()
            db["settings"]["log_channel"] = None if v.lower() == "off" else v
            save_db(db)
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Log channel: `{v}`", reply_markup=get_main_menu())
        except Exception as e:
            await message.reply_text(f"❌ {e}")

    elif action == "set_rr_delay":
        try:
            n = float(message.text.strip())
            db["settings"]["rr_delay"] = max(0.1, n)
            save_db(db)
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ .rr delay: `{n}s`", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid number.")

    elif action == "set_anti_flood":
        try:
            n = float(message.text.strip())
            db["settings"]["anti_flood_delay"] = max(0.0, n)
            save_db(db)
            USER_STATES.pop(uid, None)
            await message.reply_text(f"✅ Anti-flood: `{n}s`", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid number.")

    # ─── SEARCH USER ───
    elif action == "search_user":
        q = message.text.strip().lstrip("@")
        stats = db.get("user_stats", {})
        found = None
        try:
            tid = int(q)
            found = stats.get(str(tid))
            target = tid
        except ValueError:
            for k, v in stats.items():
                if (v.get("username") or "").lower() == q.lower():
                    found = v
                    target = int(k)
                    break
        USER_STATES.pop(uid, None)
        if not found:
            return await message.reply_text("❌ User not found.", reply_markup=get_main_menu())
        note = db.get("user_notes", {}).get(str(target), "None")
        await message.reply_text(
            f"👤 **User Profile**\n\n"
            f"ID: `{target}`\n"
            f"Name: **{found.get('first_name','?')}**\n"
            f"Username: `@{found.get('username','—')}`\n"
            f"Hosts: `{found.get('hosted_count', 0)}`\n"
            f"Bots: `{len(found.get('userbot_ids', []))}`\n"
            f"Banned: `{'Yes' if target in db['banned_users'] else 'No'}`\n"
            f"Admin: `{'Yes' if target in db['admins'] else 'No'}`\n"
            f"Sudo: `{'Yes' if target in db['sudousers'] else 'No'}`\n"
            f"📝 Note: {note}",
            reply_markup=get_main_menu()
        )

    # ─── BROADCAST ───
    elif action == "broadcast":
        if not message.text:
            return await message.reply_text("❌ Send valid text.")
        users = list(db.get("user_stats", {}).keys())
        if not users:
            USER_STATES.pop(uid, None)
            return await message.reply_text("❌ No users to broadcast to.")
        status = await message.reply_text(f"📢 Broadcasting to `{len(users)}` user(s)...")
        sent = 0
        failed = 0
        for u in users:
            try:
                await bot.send_message(int(u), f"📢 **Broadcast from Lexii Owner:**\n\n{message.text}")
                sent += 1
                await asyncio.sleep(0.15)
            except Exception:
                failed += 1
        USER_STATES.pop(uid, None)
        audit(uid, "broadcast", f"sent={sent} failed={failed}")
        await status.edit_text(
            f"✅ Broadcast: `{sent}` sent · `{failed}` failed",
            reply_markup=get_main_menu()
        )

    # ─── IMPORT DB ───
    elif action == "import_db":
        doc = message.document
        if not doc:
            return await message.reply_text("❌ Send a .json file.")
        try:
            path = await message.download(file_name=f"sessions/import_{int(time.time())}.json")
            with open(path, "r") as f:
                new_db = json.load(f)
            if not isinstance(new_db, dict) or "sessions" not in new_db:
                raise ValueError("Invalid DB format.")
            global db
            db = new_db
            save_db(db)
            os.remove(path)
            USER_STATES.pop(uid, None)
            audit(uid, "db_import", "")
            await message.reply_text("✅ DB imported successfully!", reply_markup=get_main_menu())
        except Exception as e:
            await message.reply_text(f"❌ Import failed: `{e}`")

    # ─── USERBOT HOSTING ───
    elif action == "awaiting_phone":
        if not message.text:
            return await message.reply_text("❌ Send phone number.")
        phone = message.text.strip().replace(" ", "")
        status = await message.reply_text("🔄 Connecting & requesting OTP...")
        temp = Client(f"temp_{uid}_{random.randint(1000,9999)}",
                      api_id=API_ID, api_hash=API_HASH, in_memory=True)
        try:
            await temp.connect()
            sent = await temp.send_code(phone)
            USER_STATES[uid] = {
                "action": "awaiting_otp",
                "phone": phone,
                "code_hash": sent.phone_code_hash,
                "client": temp
            }
            await status.edit_text(
                "📩 **OTP Sent!**\n\nStep 2/3: Enter OTP **with spaces** (e.g. `1 2 3 4 5`):"
            )
        except Exception as e:
            try: await temp.disconnect()
            except Exception: pass
            USER_STATES.pop(uid, None)
            await status.edit_text(f"❌ Error: `{e}`", reply_markup=get_main_menu())

    elif action == "awaiting_otp":
        if not message.text:
            return await message.reply_text("❌ Send OTP.")
        otp = re.sub(r"\D", "", message.text.strip())
        temp: Client = state["client"]
        try:
            await temp.sign_in(phone_number=state["phone"],
                                phone_code_hash=state["code_hash"],
                                phone_code=otp)
            await finalize_userbot(uid, temp, message, state.get("phone", ""))
        except SessionPasswordNeeded:
            USER_STATES[uid]["action"] = "awaiting_2fa"
            await message.reply_text("🔐 **2FA Detected!**\nStep 3/3: Send your cloud password:")
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            await message.reply_text(f"❌ Invalid OTP: `{e}`\nTry again:")
        except Exception as e:
            try: await temp.disconnect()
            except Exception: pass
            USER_STATES.pop(uid, None)
            await message.reply_text(f"❌ Login failed: `{e}`", reply_markup=get_main_menu())

    elif action == "awaiting_2fa":
        if not message.text:
            return await message.reply_text("❌ Send password.")
        temp: Client = state["client"]
        try:
            await temp.check_password(password=message.text.strip())
            await finalize_userbot(uid, temp, message, state.get("phone", ""))
        except PasswordHashInvalid:
            await message.reply_text("❌ Wrong password. Try again:")
        except Exception as e:
            try: await temp.disconnect()
            except Exception: pass
            USER_STATES.pop(uid, None)
            await message.reply_text(f"❌ Login failed: `{e}`", reply_markup=get_main_menu())


async def finalize_userbot(uid: int, temp: Client, message: Message, phone: str = ""):
    # Enforce max sessions
    max_s = db.get("settings", {}).get("max_sessions_total", 0)
    if max_s > 0 and len(db.get("sessions", [])) >= max_s:
        try: await temp.disconnect()
        except Exception: pass
        USER_STATES.pop(uid, None)
        return await message.reply_text(f"❌ Max sessions reached ({max_s}).", reply_markup=get_main_menu())

    try:
        session_str = await temp.export_session_string()
        me = await temp.get_me()
    finally:
        try: await temp.disconnect()
        except Exception: pass

    USER_STATES.pop(uid, None)
    await asyncio.sleep(1.0)
    status = await message.reply_text(f"🚀 Booting on `{me.first_name}` (`{me.id}`)...")

    try:
        await start_single_userbot(session_str)
        if session_str not in db["sessions"]:
            db["sessions"].append(session_str)

        now = time.time()
        stats = db.setdefault("user_stats", {})
        entry = stats.get(str(uid), {
            "first_name": me.first_name,
            "username": me.username or "",
            "hosted_count": 0,
            "first_host": now,
            "last_host": 0,
            "userbot_ids": []
        })
        entry["hosted_count"] = entry.get("hosted_count", 0) + 1
        entry["last_host"] = now
        entry["first_name"] = me.first_name
        entry["username"] = me.username or ""
        if me.id not in entry.get("userbot_ids", []):
            entry.setdefault("userbot_ids", []).append(me.id)
        stats[str(uid)] = entry
        db["user_stats"] = stats

        db.setdefault("host_history", []).append({
            "user_id": uid, "userbot_id": me.id,
            "userbot_name": me.first_name, "phone": phone or "N/A",
            "timestamp": now,
            "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        if len(db["host_history"]) > 500:
            db["host_history"] = db["host_history"][-500:]
        save_db(db)
        audit(uid, "userbot_host", f"{me.id}")
        await log_event(f"🚀 Userbot hosted: **{me.first_name}** (`{me.id}`) by `{uid}`")

        await status.edit_text(
            f"✅ **Lexii Userbot LIVE on `{me.first_name}` (`{me.id}`)!**\n"
            f"🤖 Swarm: `{len(ACTIVE_USERBOTS)}` bot(s)",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        await status.edit_text(f"❌ Boot failed: `{type(e).__name__}: {e}`",
                                reply_markup=get_main_menu())

# ==========================================
# 🐝 SWARM CALL HELPERS
# ==========================================
async def _single_standby(ub: dict, chat_id: int) -> bool:
    try:
        await ub["call"].play(
            chat_id,
            MediaStream(SILENT_WAV, audio_parameters=AudioQuality.HIGH,
                        ffmpeg_parameters="-stream_loop -1")
        )
        return True
    except Exception:
        return False

async def swarm_join_or_create_call(chat_id: int) -> int:
    if not ACTIVE_USERBOTS:
        return 0
    first = ACTIVE_USERBOTS[0]
    active = True
    try:
        await first["call"].play(chat_id, MediaStream(SILENT_WAV,
                                                     audio_parameters=AudioQuality.HIGH,
                                                     ffmpeg_parameters="-stream_loop -1"))
    except NoActiveGroupCall:
        active = False
    except Exception:
        pass

    if not active:
        for ub in ACTIVE_USERBOTS:
            try:
                peer = await ub["client"].resolve_peer(chat_id)
                await ub["client"].invoke(
                    CreateGroupCall(peer=peer, random_id=random.randint(10000, 999999999))
                )
                await asyncio.sleep(1.5)
                break
            except Exception:
                continue

    results = await asyncio.gather(*[_single_standby(u, chat_id) for u in ACTIVE_USERBOTS],
                                   return_exceptions=True)
    joined = sum(1 for r in results if r is True)
    if joined > 0:
        db.setdefault("active_calls", {})[str(chat_id)] = {
            "started": time.time(), "mode": "standby", "bots": joined
        }
        save_db(db)
    return joined

async def _single_blast(ub: dict, chat_id: int, wav: str) -> tuple[bool, str]:
    try:
        await ub["call"].play(
            chat_id,
            MediaStream(wav, audio_parameters=AudioQuality.HIGH,
                        ffmpeg_parameters="-stream_loop -1")
        )
        try: await ub["call"].unmute_stream(chat_id)
        except Exception: pass
        try: await ub["call"].change_volume_call(chat_id, 200)
        except Exception: pass
        return True, "OK"
    except NoActiveGroupCall:
        try:
            peer = await ub["client"].resolve_peer(chat_id)
            await ub["client"].invoke(
                CreateGroupCall(peer=peer, random_id=random.randint(10000, 999999999))
            )
            await asyncio.sleep(1.5)
            await ub["call"].play(chat_id, MediaStream(wav,
                                                       audio_parameters=AudioQuality.HIGH,
                                                       ffmpeg_parameters="-stream_loop -1"))
            return True, "Created & Playing"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

async def swarm_blast_audio(chat_id: int, wav: str) -> tuple[int, list[str]]:
    results = await asyncio.gather(*[_single_blast(u, chat_id, wav) for u in ACTIVE_USERBOTS],
                                    return_exceptions=True)
    ok = 0
    errors = []
    for r in results:
        if isinstance(r, tuple):
            o, msg = r
            if o: ok += 1
            else: errors.append(msg)
        else:
            errors.append(str(r))
    return ok, errors

async def swarm_control_action(chat_id: int, action: str):
    async def _run(ub: dict):
        cp: PyTgCalls = ub["call"]
        try:
            if action == "pe": await cp.pause_stream(chat_id)
            elif action == "rs": await cp.resume_stream(chat_id)
            elif action == "st": await cp.leave_call(chat_id)
            elif action == "vl":
                await cp.change_volume_call(chat_id, 200)
                if chat_id in ACTIVE_LOOPS:
                    await cp.play(chat_id, MediaStream(
                        ACTIVE_LOOPS[chat_id]["file"],
                        audio_parameters=AudioQuality.HIGH,
                        ffmpeg_parameters="-stream_loop -1"
                    ))
        except Exception:
            pass
    await asyncio.gather(*[_run(u) for u in ACTIVE_USERBOTS], return_exceptions=True)

# ==========================================
# 🎧 USERBOT REGISTRATION
# ==========================================
async def start_single_userbot(session_str: str):
    ub = Client(
        f"lexii_ub_{random.randint(10000, 99999)}",
        api_id=API_ID, api_hash=API_HASH,
        session_string=session_str, in_memory=True
    )
    call_py = PyTgCalls(ub)

    @call_py.on_update()
    async def on_stream_end(client: PyTgCalls, update: Any):
        is_end = (StreamEndEvent and isinstance(update, StreamEndEvent)) or \
                 type(update).__name__ in ("StreamAudioEnded", "StreamEnded")
        if is_end:
            cid = getattr(update, "chat_id", None)
            if not cid: return
            loop = ACTIVE_LOOPS.get(cid)
            if loop and loop.get("loop"):
                try:
                    await client.play(cid, MediaStream(
                        loop["file"], audio_parameters=AudioQuality.HIGH,
                        ffmpeg_parameters="-stream_loop -1"
                    ))
                    await client.change_volume_call(cid, 200)
                except Exception:
                    pass

    @ub.on_message(filters.text & ~filters.private)
    async def group_cmd(client: Client, message: Message):
        sender = message.from_user.id if message.from_user else 0
        if not (message.outgoing or is_authorized(sender)):
            return
        if sender in db.get("banned_users", []) or message.chat.id in db.get("banned_groups", []):
            return

        key = (message.chat.id, message.id)
        if key in PROCESSED_MSGS: return
        PROCESSED_MSGS.add(key)
        if len(PROCESSED_MSGS) > 2000: PROCESSED_MSGS.clear()

        raw = message.text.strip()
        txt = raw.lower()
        cid = message.chat.id

        # track group stats
        gstats = db.setdefault("group_stats", {}).setdefault(str(cid), {"commands": 0})
        gstats["commands"] = gstats.get("commands", 0) + 1

        if txt == "hey lexii":
            await message.reply_text("yess dear how can i help you??")

        elif txt == "menu":
            await message.reply_text(
                "🎛 **LEXII SWARM MENU**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🔥 `-xtrem` · 🌊 `-flow` · ⚡ `-adv` · 🌀 `-dynamic`\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🔗 `join <link>` · 🐝 `cll` ({len(ACTIVE_USERBOTS)} bots)\n"
                "🎛 `pe` `rs` `st` `vl`\n"
                "🎯 `.rr` / `.srr`\n"
                "👑 `admin all`"
            )

        elif txt == ".rr" or txt.startswith(".rr "):
            if not db.get("rr_texts"):
                return await message.reply_text("⚠️ No `.rr` texts. Add in Owner Panel.")
            tgt_id = 0; tgt_name = "Target"; reply_id = None
            if message.reply_to_message:
                reply_id = message.reply_to_message.id
                if message.reply_to_message.from_user:
                    tgt_id = message.reply_to_message.from_user.id
                    tgt_name = message.reply_to_message.from_user.first_name or "Target"
                elif message.reply_to_message.sender_chat:
                    tgt_name = message.reply_to_message.sender_chat.title or "Channel"
            elif len(raw.split(maxsplit=1)) > 1:
                arg = raw.split(maxsplit=1)[1].strip()
                try:
                    u = await client.get_users(arg)
                    tgt_id = u.id; tgt_name = u.first_name or arg
                except Exception:
                    tgt_name = arg
            else:
                return await message.reply_text(
                    "⚠️ Reply to a user with `.rr` or type `.rr @username`"
                )

            if cid in ACTIVE_RR_TASKS:
                ACTIVE_RR_TASKS[cid]["active"] = False
                old = ACTIVE_RR_TASKS[cid].get("task")
                if old and not old.done(): old.cancel()

            ACTIVE_RR_TASKS[cid] = {"active": True, "task": None}
            t = asyncio.create_task(_rr_swipe_loop(cid, tgt_id, tgt_name, reply_id))
            ACTIVE_RR_TASKS[cid]["task"] = t

        elif txt == ".srr":
            if cid in ACTIVE_RR_TASKS and ACTIVE_RR_TASKS[cid].get("active"):
                ACTIVE_RR_TASKS[cid]["active"] = False
                t = ACTIVE_RR_TASKS[cid].get("task")
                if t and not t.done(): t.cancel()
                ACTIVE_RR_TASKS.pop(cid, None)
                await message.reply_text("🛑 `.rr` raid stopped.")
            else:
                await message.reply_text("ℹ️ No active raid.")

        elif txt.startswith("join "):
            link = raw.split(maxsplit=1)[1].strip()
            st = await message.reply_text(f"🐝 Joining `{len(ACTIVE_USERBOTS)}` bot(s)...")
            ok, det = await swarm_join_group(link)
            await st.edit_text(
                f"🔗 **Report ({ok}/{len(ACTIVE_USERBOTS)})**\n\n" + "\n".join(det)
            )

        elif txt == "cll":
            ok = await swarm_join_or_create_call(cid)
            if ok > 0:
                await message.reply_text(f"next what can i do\n*(🐝 `{ok}` bot(s) joined the call)*")
            else:
                await message.reply_text("❌ Cannot join call. Check admin permissions.")

        elif txt in ["-xtrem", "-xtream", "-flow", "-adv", "-dynamic"]:
            mode = "-xtrem" if txt == "-xtream" else txt
            audio = db["audios"].get(mode) or db["audios"].get("default")
            if not audio or not os.path.exists(audio):
                return await message.reply_text("⚠️ No audio uploaded yet.")
            if not audio.endswith(".wav") or "processed" not in audio:
                loop = asyncio.get_running_loop()
                audio = await loop.run_in_executor(None, build_boosted_wav, audio, mode)
                db["audios"][mode] = audio
                save_db(db)

            ACTIVE_LOOPS[cid] = {"file": audio, "mode": mode, "loop": True}
            db.setdefault("mode_usage", {})[mode] = db["mode_usage"].get(mode, 0) + 1
            db.setdefault("active_calls", {})[str(cid)] = {
                "started": db.get("active_calls", {}).get(str(cid), {}).get("started", time.time()),
                "mode": mode, "bots": len(ACTIVE_USERBOTS)
            }
            save_db(db)
            count, errs = await swarm_blast_audio(cid, audio)
            if count > 0:
                await message.reply_text(
                    f"🔊 **Blasting `{mode}`**\n🐝 `{count}` bot(s) · ♾ Loop · 🎚 200% volume"
                )
            else:
                await message.reply_text(f"❌ Playback failed: `{errs[0] if errs else 'Unknown'}`")

        elif txt == "pe":
            await swarm_control_action(cid, "pe")
            await message.reply_text("⏸ Paused. Say `rs` to resume.")

        elif txt == "rs":
            await swarm_control_action(cid, "rs")
            await message.reply_text("▶️ Resumed!")

        elif txt == "st":
            if cid in ACTIVE_LOOPS:
                ACTIVE_LOOPS[cid]["loop"] = False
                ACTIVE_LOOPS.pop(cid, None)
            db.get("active_calls", {}).pop(str(cid), None)
            save_db(db)
            await swarm_control_action(cid, "st")
            await message.reply_text("⏹ Stopped. All bots left the call.")

        elif txt == "vl":
            base = ACTIVE_LOOPS.get(cid, {}).get("file") or db["audios"].get("default") or db["audios"].get("-xtrem")
            if base and os.path.exists(base):
                loop = asyncio.get_running_loop()
                vlw = await loop.run_in_executor(None, build_boosted_wav, base, "vl_max")
                ACTIVE_LOOPS[cid] = {"file": vlw, "mode": "vl_max", "loop": True}
            await swarm_control_action(cid, "vl")
            await message.reply_text("🔊 MAX OVERDRIVE!")

        elif txt == "admin all" and (sender == OWNER_ID or message.outgoing):
            db["public_admin_mode"] = True
            save_db(db)
            await message.reply_text("👑 Granted Lexii Admin to EVERYONE!")
            promoted = 0
            try:
                async for m in client.get_chat_members(cid):
                    if not m.user.is_bot and m.status.name == "MEMBER":
                        try:
                            await client.promote_chat_member(cid, m.user.id, privileges=ChatPrivileges(
                                can_manage_chat=True, can_manage_video_chats=True, can_invite_users=True
                            ))
                            promoted += 1
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
            except Exception:
                pass
            if promoted: await message.reply_text(f"✅ Promoted `{promoted}` members.")

    if not ub.is_connected:
        await ub.start()
    await call_py.start()
    me = await ub.get_me()

    if me.id not in db["admins"]:
        db["admins"].append(me.id)
        save_db(db)

    async def _warm():
        try:
            async for _ in ub.get_dialogs(limit=50): pass
        except Exception: pass
    asyncio.create_task(_warm())

    ACTIVE_USERBOTS[:] = [u for u in ACTIVE_USERBOTS if u["id"] != me.id]
    ACTIVE_USERBOTS.append({
        "client": ub, "call": call_py,
        "id": me.id, "name": me.first_name
    })
    print(f"✅ Userbot `{me.first_name}` ({me.id}) online.")

# ==========================================
# 🔄 SESSION MONITOR
# ==========================================
async def session_monitor():
    while True:
        try:
            await asyncio.sleep(300)
            for ub in list(ACTIVE_USERBOTS):
                try:
                    if not ub["client"].is_connected:
                        print(f"🔄 Reconnecting {ub['name']}...")
                        await ub["client"].start()
                except Exception as e:
                    print(f"⚠️ Reconnect failed {ub['name']}: {e}")
        except asyncio.CancelledError:
            break
        except Exception:
            pass

# ==========================================
# 🌐 HEALTH SERVER
# ==========================================
async def handle_health(reader, writer):
    await reader.read(1024)
    body = json.dumps({
        "status": "alive", "active_bots": len(ACTIVE_USERBOTS),
        "uptime": int(time.time() - db.get("started_at", time.time()))
    })
    resp = (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}")
    writer.write(resp.encode())
    await writer.drain()
    writer.close()

async def start_health():
    srv = await asyncio.start_server(handle_health, "0.0.0.0", PORT)
    print(f"🌐 Health on port {PORT}")
    async with srv:
        await srv.serve_forever()

# ==========================================
# 🚀 MAIN
# ==========================================
async def main():
    asyncio.create_task(start_health())
    asyncio.create_task(session_monitor())

    print("🔄 Starting Controller Bot...")
    await bot.start()
    print(f"🔄 Loading {len(db['sessions'])} session(s)...")

    valid = []
    for s in db["sessions"]:
        try:
            await start_single_userbot(s)
            valid.append(s)
        except Exception as e:
            print(f"⚠️ Dropped dead session: {e}")

    if len(valid) != len(db["sessions"]):
        db["sessions"] = valid
        save_db(db)

    await log_event(f"✅ Lexii v3.0 Online · {len(ACTIVE_USERBOTS)} bot(s)")
    print(f"✅ Lexii v3.0 online with {len(ACTIVE_USERBOTS)} bot(s)!")
    await idle()
    await bot.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
