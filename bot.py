import discord
from discord.ext import commands
from discord import app_commands
import datetime
import asyncio
from flask import Flask
from threading import Thread
import os
import json
import tempfile
import traceback

# ================= [ Render 24시간 가동을 위한 웹서버 ] =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running perfectly!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()

# ================= [ 데이터베이스 관리 로직 ] =================
# Render 등에서 Persistent Disk를 쓰면 DATA_DIR 환경변수에 마운트 경로(예: /data)를 넣어 주세요.
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "guild_settings.json")
COMMAND_VERSION = 3   # 전역 명령어 구성이 바뀔 때만 올리면 시작 시 1회 동기화됩니다.
META = {}

def load_settings():
    global META
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("_meta")
        META = meta if isinstance(meta, dict) else {}
        return {int(k): v for k, v in data.items() if str(k).isdigit()}
    except Exception as e:
        print(f"❌ 데이터 로드 중 오류 발생 (초기화 진행): {e}")
        return {}

def save_settings(settings):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data_to_save = {str(k): v for k, v in settings.items()}
        data_to_save["_meta"] = META
        dir_name = os.path.dirname(DATA_FILE) or '.'
        with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
            json.dump(data_to_save, tf, ensure_ascii=False, indent=4)
            temp_name = tf.name
        os.replace(temp_name, DATA_FILE)
    except Exception as e:
        print(f"❌ 데이터 저장 중 치명적 오류 발생: {e}")

GUILD_SETTINGS = load_settings()

def get_gs(guild_id: int) -> dict:
    return GUILD_SETTINGS.setdefault(guild_id, {})

def enabled_keys(guild_id: int) -> set:
    return set(GUILD_SETTINGS.get(guild_id, {}).get("enabled", []))

def is_on(guild_id: int, key: str) -> bool:
    return key in GUILD_SETTINGS.get(guild_id, {}).get("enabled", [])

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class NattoBot(commands.Bot):
    async def setup_hook(self):
        # 처음에는 전역 명령어로 /활성화 하나만 등록합니다.
        self.tree.add_command(build_activation_group())
        # 이미 기능을 켜 둔 서버의 명령어는 (서버 쪽에 등록돼 있으므로) 로컬 트리에만 복원합니다.
        for gid, gs in list(GUILD_SETTINGS.items()):
            if gs.get("enabled"):
                register_guild_commands(gid)

bot = NattoBot(command_prefix="!", intents=intents)

# ================= [ 공통 헬퍼 ] =================
def format_member_name(member: discord.Member) -> str:
    """서버 닉네임(본 닉네임) 형태로 표기. 두 이름이 같으면 한 번만 표기."""
    server_nick = member.display_name
    original_nick = member.global_name or member.name
    def clean(text: str) -> str:
        return discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    if server_nick == original_nick:
        return clean(server_nick)
    return f"{clean(server_nick)} ({clean(original_nick)})"

def josa(word: str, with_final: str, without_final: str) -> str:
    """마지막 글자의 받침 유무에 따라 조사를 선택합니다."""
    last = word.strip()[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 != 0 else without_final
    return without_final

def safe_text(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))

admin_only = app_commands.checks.has_permissions(administrator=True)

def setup_hint(guild_id: int, key: str) -> str:
    f = FEATURES[key]
    setup = f"`/설정 『{f['cat']}』 {key}`"
    if is_on(guild_id, key):
        return f"먼저 {setup} 명령어로 설정해 주세요."
    return f"먼저 `/활성화 『{f['cat']}』 {key}` 로 활성화한 뒤 {setup} 명령어로 설정해 주세요."

async def require_setting_channel(interaction: discord.Interaction, key: str, action: str) -> bool:
    """필수 채널 설정이 없으면 안내 문구를 남기고 False를 반환합니다."""
    guild = interaction.guild
    f = FEATURES[key]
    label = f["label"]
    channel_id = GUILD_SETTINGS.get(guild.id, {}).get(f["store"])
    if not channel_id:
        await interaction.response.send_message(
            f"⚠️ **{label}**{josa(label, '이', '가')} 설정되어 있지 않아 {action}{josa(action, '을', '를')} 실행할 수 없습니다.\n{setup_hint(guild.id, key)}",
            ephemeral=True
        )
        return False
    if guild.get_channel(channel_id) is None:
        await interaction.response.send_message(
            f"⚠️ 설정된 **{label}**{josa(label, '을', '를')} 찾을 수 없습니다. (채널이 삭제되었거나 봇이 볼 수 없는 채널입니다)\n{setup_hint(guild.id, key)}",
            ephemeral=True
        )
        return False
    return True

# ---------- ✅/❌ 반응으로 한 번 더 확인하는 공통 로직 ----------
CONFIRM_TIMEOUT = 60

async def confirm_by_reaction(interaction: discord.Interaction, warn_text: str, action: str) -> bool:
    """경고 메시지를 띄우고 ✅면 True, ❌/시간초과/오류면 False를 반환합니다. (명령어 입력자만 선택 가능)"""
    await interaction.response.send_message(warn_text)
    warn_msg = await interaction.original_response()

    async def delete_warning():
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def notice(text: str):
        try:
            await interaction.channel.send(text, delete_after=5)
        except (discord.HTTPException, AttributeError):
            pass

    try:
        await warn_msg.add_reaction("✅")
        await warn_msg.add_reaction("❌")
    except discord.HTTPException:
        await delete_warning()
        await interaction.followup.send("❌ 반응(이모지)을 추가할 권한이 없어 진행할 수 없습니다. 봇에 '반응 추가하기', '메시지 기록 보기' 권한을 주세요.", ephemeral=True)
        return False

    def check(payload: discord.RawReactionActionEvent):
        return (
            payload.message_id == warn_msg.id
            and payload.user_id == interaction.user.id
            and str(payload.emoji) in ("✅", "❌")
        )

    try:
        payload = await bot.wait_for("raw_reaction_add", check=check, timeout=CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        await delete_warning()
        await notice(f"⏰ {interaction.user.mention} 시간이 초과되어 {action}{josa(action, '이', '가')} 취소되었습니다.")
        return False

    await delete_warning()
    if str(payload.emoji) == "✅":
        return True
    await notice(f"🚫 {interaction.user.mention} {action}{josa(action, '이', '가')} 거절(취소)되었습니다. 기존 상태는 그대로 유지됩니다.")
    return False

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str, punisher: str = "시스템 자동 검열") -> bool:
    if not guild or not member:
        return False

    log_channel = None
    log_channel_id = GUILD_SETTINGS.get(guild.id, {}).get("punish_log_channel")
    if log_channel_id:
        log_channel = guild.get_channel(log_channel_id)

    async def send_log(**kwargs):
        if not log_channel:
            return
        try:
            await log_channel.send(**kwargs)
        except discord.HTTPException:
            pass

    if member.top_role >= guild.me.top_role:
        embed = discord.Embed(title="❌ 제재 권한 실패 경고", color=discord.Color.dark_red())
        embed.description = f"봇의 역할 순위가 대상 유저({member.mention})보다 낮거나 같아 제재할 수 없습니다."
        await send_log(embed=embed)
        return False

    current_count = 0
    roles_to_remove = []
    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            try:
                current_count = int(r.name.replace("전과 ", "").replace("범", "").strip())
                roles_to_remove.append(r)
            except ValueError:
                continue

    current_count += 1

    try:
        if current_count >= 20:
            await member.ban(reason=f"전과 20범 달성 ({reason})"[:512])
            embed = discord.Embed(title="🚨 유저 영구 차단 (BAN)", color=discord.Color.red(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
            embed.add_field(name="💬 최종 사유", value=reason[:1024], inline=False)
            await send_log(embed=embed)
            return True

        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        await member.edit(timed_out_until=target_time, reason=f"전과 {current_count}범 제재 ({reason})"[:512])

        role_name = f"전과 {current_count}범"
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")
            except discord.HTTPException as e:
                await send_log(content=f"❌ **역할 생성 실패 (서버 제한 초과 가능성):** `{e.text}`")
                return True

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        embed = discord.Embed(title="⚠️ 유저 제재 알림", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
        embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
        embed.add_field(name="📜 조치 내용", value=f"**{role_name}** 부여 및 **{punish_days}일간** 타임아웃", inline=False)
        embed.add_field(name="💬 제재 사유", value=reason[:1024], inline=False)
        await send_log(embed=embed)
        return True

    except discord.Forbidden:
        embed = discord.Embed(title="❌ 제재 권한 실패 경고", color=discord.Color.dark_red())
        embed.description = f"봇의 역할 순위가 {member.mention}님보다 낮아 제재 처리에 실패했습니다. 서버 설정에서 봇 역할을 위로 올려주세요."
        await send_log(embed=embed)
        return False
    except Exception as e:
        await send_log(content=f"❌ **제재 실행 중 시스템 에러 발생:** `{e}`")
        return False

async def send_punish_log(guild: discord.Guild, embed: discord.Embed):
    """제재 로그 채널이 설정돼 있으면 기록을 남깁니다. (없어도 본 작업은 정상 처리)"""
    log_channel_id = GUILD_SETTINGS.get(guild.id, {}).get("punish_log_channel")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if log_channel:
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

# ================= [ 이벤트 처리 ] =================
_startup_done = False

@bot.event
async def on_ready():
    global _startup_done
    print(f"🤖 {bot.user.name} 봇이 가동되었습니다.")
    if _startup_done:
        return
    _startup_done = True

    force = os.getenv("SYNC_COMMANDS") == "1"
    # 명령어 동기화는 요청 수가 많아 IP 차단(레이트 리밋)의 원인이 되므로, 필요할 때만 합니다.
    if force or META.get("synced_version") != COMMAND_VERSION:
        try:
            synced = await bot.tree.sync()
            META["synced_version"] = COMMAND_VERSION
            save_settings(GUILD_SETTINGS)
            print(f"🔗 전역 슬래시 명령어 {len(synced)}개 동기화 완료")
        except Exception as e:
            print(f"❌ 슬래시 명령어 동기화 실패: {e}")
    if force:
        for gid, gs in list(GUILD_SETTINGS.items()):
            guild = bot.get_guild(gid)
            if guild and gs.get("enabled"):
                try:
                    await bot.tree.sync(guild=discord.Object(id=gid))
                except Exception as e:
                    print(f"❌ {gid} 서버 명령어 동기화 실패: {e}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if perms.send_messages and perms.create_public_threads:
            try:
                msg = await channel.send("🛡️ **낫또봇이 입장했습니다!**")
                thread = await msg.create_thread(
                    name="🛡️ 낫또봇 시작 가이드",
                    auto_archive_duration=1440,
                    reason="초기 입장 가이드 제공"
                )
                embed = discord.Embed(
                    title="⚙️ 낫또봇 시작하기",
                    description="처음에는 **`/활성화`** 명령어 하나만 보여요. 필요한 기능만 골라 켜서 쓰세요!",
                    color=discord.Color.blurple()
                )
                embed.add_field(
                    name="1️⃣ 기능 켜기",
                    value="`/활성화` → 카테고리(인증·제재·입퇴장·청소·기타) → 기능 선택 → Enter\n카테고리의 `전체`를 고르면 한 번에 켤 수 있어요.",
                    inline=False
                )
                embed.add_field(
                    name="2️⃣ 설정하기",
                    value="`/설정` → 카테고리 → 기능 → 값 입력\n(켜 둔 기능만 목록에 나타나요)",
                    inline=False
                )
                embed.add_field(
                    name="📖 자세한 사용법",
                    value="`/활성화 기타 가이드` 로 가이드를 켠 뒤 `/가이드`를 입력하세요.",
                    inline=False
                )
                await thread.send(embed=embed)
                break
            except Exception:
                continue

@bot.event
async def on_member_join(member: discord.Member):
    guild_id = member.guild.id
    settings = GUILD_SETTINGS.get(guild_id)
    if not settings:
        return

    auth_channel_id = settings.get("auth_channel")
    auth_form = settings.get("auth_form")

    if is_on(guild_id, "인증방") and auth_channel_id and auth_form:
        auth_channel = member.guild.get_channel(auth_channel_id)
        if auth_channel:
            converted_form = auth_form.replace("\\n", "\n")
            try:
                await auth_channel.send(f"👋 {member.mention}님, 서버에 오신 것을 환영합니다!\n{converted_form}")
            except discord.HTTPException:
                pass

    join_log_id = settings.get("join_log_channel")
    if is_on(guild_id, "입장로그") and join_log_id:
        join_log_channel = member.guild.get_channel(join_log_id)
        if join_log_channel:
            now = datetime.datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분')
            created_at = member.created_at.strftime('%Y년 %m월 %d일 %H시 %M분')
            embed = discord.Embed(title="📥 유저 서버 입장", color=discord.Color.green())
            embed.add_field(name="👤 대상 유저", value=f"{member.mention} ({member.name})", inline=False)
            embed.add_field(name="⏰ 입장 시간", value=now, inline=True)
            embed.add_field(name="📅 계정 생성일", value=created_at, inline=True)
            embed.set_thumbnail(url=(member.avatar or member.default_avatar).url)
            try:
                await join_log_channel.send(embed=embed)
            except discord.HTTPException:
                pass

@bot.event
async def on_member_remove(member: discord.Member):
    guild_id = member.guild.id
    if not is_on(guild_id, "퇴장로그"):
        return
    leave_log_id = GUILD_SETTINGS.get(guild_id, {}).get("leave_log_channel")
    if leave_log_id:
        leave_log_channel = member.guild.get_channel(leave_log_id)
        if leave_log_channel:
            embed = discord.Embed(title="📤 유저 서버 퇴장", color=discord.Color.red())
            embed.add_field(name="👤 퇴장 유저", value=f"{member.name} (ID: {member.id})", inline=False)
            embed.set_thumbnail(url=(member.avatar or member.default_avatar).url)
            try:
                await leave_log_channel.send(embed=embed)
            except discord.HTTPException:
                pass

# ================= [ 실시간 문자 검열 및 자동 제재 엔진 ] =================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    settings = GUILD_SETTINGS.get(guild_id, {})

    if is_on(guild_id, "제재단어"):
        for word in settings.get("ban_words", []):
            if word in message.content:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                reason = f"자동 제재 대상 단어 [{word}] 포함 문장 작성"
                await punish_user(message.guild, message.author, f"{reason}\n포착된 메시지: ||{message.content}||", "시스템 자동 검열")
                return

    if is_on(guild_id, "검열단어"):
        for word in settings.get("censor_words", []):
            if word in message.content:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                await message.channel.send(f"⚠️ {message.author.mention}님의 메시지에 검열 대상 단어가 포함되어 스포일러 처리되었습니다:\n||{message.content}||")
                return

    await bot.process_commands(message)

# ================= [ 슬래시 명령어 전역 에러 핸들러 ] =================
async def _reply(interaction: discord.Interaction, text: str):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await _reply(interaction, "❌ 이 명령어를 사용할 권한이 없습니다.")
    elif isinstance(error, app_commands.BotMissingPermissions):
        await _reply(interaction, "❌ 봇에게 필요한 권한이 부족하여 명령어를 실행할 수 없습니다.")
    elif isinstance(error, app_commands.CommandNotFound):
        await _reply(interaction, "⚠️ 이 명령어는 현재 비활성화 상태입니다. `/활성화` 로 다시 켜 주세요.")
    elif isinstance(error, app_commands.CheckFailure):
        await _reply(interaction, "❌ 이 명령어를 사용할 수 없습니다.")
    else:
        original = getattr(error, "original", error)
        print("슬래시 명령어 오류 발생:")
        traceback.print_exception(type(original), original, original.__traceback__)
        await _reply(interaction, "❌ 명령어를 처리하는 중 알 수 없는 시스템 오류가 발생했습니다.")

# ====================================================================
# ⚙️ 설정 · 제거 · 체크 · 초기화 콜백 (팩토리)
# ====================================================================

# ---------- 채널 설정 ----------
def make_set_channel(store: str, label: str):
    @app_commands.describe(채널=f"{label}에 사용할 텍스트 채널을 선택하세요.")
    @admin_only
    async def cb(interaction: discord.Interaction, 채널: discord.TextChannel):
        get_gs(interaction.guild.id)[store] = 채널.id
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"✅ {label}{josa(label, '이', '가')} {채널.mention}(으)로 설정되었습니다.", ephemeral=True)
    return cb

def make_remove_channel(store: str, label: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        gs = GUILD_SETTINGS.get(interaction.guild.id, {})
        if store in gs:
            del gs[store]
            save_settings(GUILD_SETTINGS)
            await interaction.response.send_message(f"🗑️ {label} 설정이 완전히 제거되었습니다.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 설정된 {label}{josa(label, '이', '가')} 없습니다.", ephemeral=True)
    return cb

def make_check_channel(store: str, label: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        channel_id = GUILD_SETTINGS.get(interaction.guild.id, {}).get(store)
        if not channel_id:
            await interaction.response.send_message(f"❌ 설정된 {label}{josa(label, '이', '가')} 없습니다.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        channel_text = channel.mention if channel else "(삭제되었거나 접근할 수 없는 채널)"
        await interaction.response.send_message(f"📌 **현재 {label}:** {channel_text}\n🆔 채널 ID: `{channel_id}`", ephemeral=True)
    return cb

# ---------- 인증 지급 역할 ----------
@app_commands.describe(역할="지급할 기본 역할을 선택하세요. 여러 번 실행하면 다중 등록됩니다.")
@admin_only
async def set_auth_role_cb(interaction: discord.Interaction, 역할: discord.Role):
    if 역할 >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ 봇의 서열과 같거나 더 높은 역할은 자동 지급용으로 등록할 수 없습니다.", ephemeral=True)
        return
    role_ids = get_gs(interaction.guild.id).setdefault("auth_role_ids", [])
    if 역할.id not in role_ids:
        role_ids.append(역할.id)
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"✅ 기본 마스터 역할 목록에 **{safe_text(역할.name)}**이(가) 추가되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ **{safe_text(역할.name)}**은(는) 이미 기본 역할 목록에 등록되어 있습니다.", ephemeral=True)

async def auth_role_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if not guild:
        return []
    cur = current.lower()
    choices = []
    for role_id in GUILD_SETTINGS.get(guild.id, {}).get("auth_role_ids", []):
        role = guild.get_role(role_id)
        label = role.name if role else f"(삭제된 역할) {role_id}"
        if cur and cur not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(role_id)))
    return choices[:25]

@app_commands.describe(역할="인증지급에서 제외할 역할을 목록에서 선택하세요.")
@app_commands.autocomplete(역할=auth_role_autocomplete)
@admin_only
async def remove_auth_role_cb(interaction: discord.Interaction, 역할: str):
    guild = interaction.guild
    role_ids = GUILD_SETTINGS.get(guild.id, {}).get("auth_role_ids", [])
    if not role_ids:
        await interaction.response.send_message("❌ 인증지급에 등록된 역할이 없습니다.", ephemeral=True)
        return
    if not 역할.isdigit() or int(역할) not in role_ids:
        await interaction.response.send_message("❌ 선택한 역할은 인증지급에 등록되어 있지 않습니다. 목록에서 선택해 주세요.", ephemeral=True)
        return
    role_id = int(역할)
    role_ids.remove(role_id)
    save_settings(GUILD_SETTINGS)
    role = guild.get_role(role_id)
    role_name = safe_text(role.name) if role else f"삭제된 역할({role_id})"
    await interaction.response.send_message(f"🗑️ **{role_name}** 역할이 인증지급에서 제외되었습니다. (남은 역할: {len(role_ids)}개)", ephemeral=True)

@admin_only
async def check_auth_role_cb(interaction: discord.Interaction):
    guild = interaction.guild
    role_ids = GUILD_SETTINGS.get(guild.id, {}).get("auth_role_ids", [])
    if not role_ids:
        await interaction.response.send_message("❌ 인증지급에 등록된 역할이 없습니다.", ephemeral=True)
        return
    lines = []
    for rid in role_ids:
        role = guild.get_role(rid)
        lines.append(role.mention if role else f"(삭제된 역할) `{rid}`")
    embed = discord.Embed(title=f"🎖️ 인증지급 역할 ({len(role_ids)}개)", description="\n".join(lines)[:4000], color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- 인증 안내 메세지 ----------
@app_commands.describe(메세지="인증방에 출력할 문구입니다. 줄바꿈은 \\n 을 입력하세요.")
@admin_only
async def set_auth_form_cb(interaction: discord.Interaction, 메세지: str):
    get_gs(interaction.guild.id)["auth_form"] = 메세지
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message("✅ 인증 안내 메세지가 정상적으로 저장되었습니다.", ephemeral=True)

@admin_only
async def check_auth_form_cb(interaction: discord.Interaction):
    form = GUILD_SETTINGS.get(interaction.guild.id, {}).get("auth_form")
    if not form:
        await interaction.response.send_message("❌ 등록된 인증 메세지가 없습니다.", ephemeral=True)
        return
    preview = form.replace("\\n", "\n")
    if len(preview) > 3900:
        preview = preview[:3900] + "\n…(일부 생략)"
    embed = discord.Embed(title="📝 등록된 인증 메세지", description=preview, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- 제재/검열 단어 ----------
def make_set_words(key: str, label: str, done_text: str):
    @app_commands.describe(단어=f"{label}를 입력하세요. 여러 개는 쉼표(,)로 구분해 한 번에 넣을 수 있습니다.")
    @admin_only
    async def cb(interaction: discord.Interaction, 단어: str):
        words = get_gs(interaction.guild.id).setdefault(key, [])
        added = []
        for w in [w.strip() for w in 단어.split(",") if w.strip()]:
            if w not in words:
                words.append(w)
                added.append(w)
        if not added:
            await interaction.response.send_message(f"⚠️ 입력한 {label}는 이미 모두 등록되어 있거나 유효하지 않습니다.", ephemeral=True)
            return
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"{done_text} `{', '.join(added)}` 가 서버 필터에 누적 등록되었습니다.", ephemeral=True)
    return cb

def make_word_autocomplete(key: str):
    async def ac(interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        cur = current.lower()
        words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
        return [app_commands.Choice(name=w[:100], value=w) for w in words if cur in w.lower() and len(w) <= 100][:25]
    return ac

def make_remove_word(key: str, label: str):
    @app_commands.describe(단어=f"삭제할 {label}를 목록에서 선택하세요.")
    @app_commands.autocomplete(단어=make_word_autocomplete(key))
    @admin_only
    async def cb(interaction: discord.Interaction, 단어: str):
        words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
        if 단어 not in words:
            await interaction.response.send_message(f"❌ `{단어}` 은(는) 등록된 {label}가 아닙니다. 목록에서 선택해 주세요.", ephemeral=True)
            return
        words.remove(단어)
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"🗑️ {label} `{단어}` 이(가) 삭제되었습니다. (남은 단어: {len(words)}개)", ephemeral=True)
    return cb

def make_check_words(key: str, label: str, emoji: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
        if not words:
            await interaction.response.send_message(f"❌ 등록된 {label}{josa(label, '이', '가')} 없습니다.", ephemeral=True)
            return
        word_text = ", ".join(f"`{w}`" for w in words)
        if len(word_text) > 3900:
            word_text = word_text[:3900] + " …(일부 생략)"
        embed = discord.Embed(title=f"{emoji} 등록된 {label} ({len(words)}개)", description=word_text, color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return cb

# ---------- 목록 초기화 (✅/❌ 2차 확인) ----------
def make_reset(key: str, label: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        items = GUILD_SETTINGS.get(guild_id, {}).get(key, [])
        if not items:
            await interaction.response.send_message(f"❌ 등록된 {label}{josa(label, '이', '가')} 없어 초기화할 내용이 없습니다.", ephemeral=True)
            return
        warn_text = (
            f"⚠️ **경고: {label} 전체 초기화**\n"
            f"{interaction.user.mention}님, 등록된 {label} **{len(items)}개**가 모두 삭제되며 되돌릴 수 없습니다.\n\n"
            f"✅ 를 누르면 **초기화 진행** / ❌ 를 누르면 **취소**\n"
            f"(명령어를 입력한 본인만 선택할 수 있으며, {CONFIRM_TIMEOUT}초 안에 선택하지 않으면 자동 취소됩니다.)"
        )
        if not await confirm_by_reaction(interaction, warn_text, f"{label} 초기화"):
            return
        gs = GUILD_SETTINGS.get(guild_id, {})
        if gs.get(key):
            gs[key] = []
            save_settings(GUILD_SETTINGS)
        try:
            await interaction.followup.send(f"🗑️ 등록된 {label} {len(items)}개가 모두 초기화되었습니다.", ephemeral=True)
        except discord.HTTPException:
            pass
    return cb

# ====================================================================
# 🛠️ 개별 기능 명령어 (/인증완료 /제재 /제재지우기 /추방 /추방해제 /청소 /서버청소 /가이드)
# ====================================================================

@app_commands.describe(유저="인증을 승인할 유저를 선택하세요.", 추가역할="성별이나 추가로 줄 특수 역할을 선택하세요. (선택사항)")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
async def approve_user_cb(interaction: discord.Interaction, 유저: discord.Member, 추가역할: discord.Role = None):
    if not await require_setting_channel(interaction, "인증방", "인증 승인"):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if 유저.top_role >= guild.me.top_role:
        await interaction.followup.send("❌ 봇의 서열이 대상 유저보다 낮거나 같아 역할을 지급할 수 없습니다.")
        return
    if 추가역할 and 추가역할 >= guild.me.top_role:
        await interaction.followup.send("❌ 지급하려는 추가 역할의 서열이 봇의 서열보다 높거나 같아 지급할 수 없습니다.")
        return

    settings = GUILD_SETTINGS.get(guild.id, {})
    base_role_ids = settings.get("auth_role_ids", [])
    auth_channel_id = settings.get("auth_channel")

    if not base_role_ids and not 추가역할:
        await interaction.followup.send("❌ 지급할 기본 역할이나 추가역할이 없습니다. `/설정 인증 인증지급` 으로 먼저 등록해 주세요.")
        return

    roles_to_add = []
    for r_id in base_role_ids:
        role = guild.get_role(r_id)
        if role and role < guild.me.top_role:
            roles_to_add.append(role)
    if 추가역할:
        roles_to_add.append(추가역할)

    if not roles_to_add:
        await interaction.followup.send("❌ 지정된 역할들이 존재하지 않거나 봇의 권한보다 높아서 지급할 수 없습니다.")
        return

    try:
        await 유저.add_roles(*roles_to_add, reason="관리자 수동 인증 완료 승인")
        target_channel = guild.get_channel(auth_channel_id) or interaction.channel
        if target_channel:
            roles_text = ", ".join(f"**{safe_text(r.name)}**" for r in roles_to_add)
            await target_channel.send(f"🎉 축하합니다 {유저.mention}님! 서류 인증이 승인되어 멤버 역할({roles_text}) 자격이 활성화되었습니다.")
        await interaction.followup.send(f"✅ {safe_text(유저.name)}님에게 성공적으로 {len(roles_to_add)}개의 역할을 지급하고 승인했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 서열(역할 순위)이 타겟 유저 또는 지급 역할보다 낮아 처리에 실패했습니다.")

@app_commands.describe(멤버="제재할 멤버를 선택하세요.", 사유="제재 사유를 입력하세요.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def manual_punish_cb(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 제재"):
    if not await require_setting_channel(interaction, "제재로그", "제재"):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    display = format_member_name(멤버)

    if 멤버.top_role >= guild.me.top_role:
        await interaction.followup.send(f"❌ **제재 실패:** {display}(서열 서치 불가)")
        return

    ok = await punish_user(guild, 멤버, 사유, str(interaction.user.name))
    if ok:
        await interaction.followup.send(f"✅ **제재 완료:** {display}")
    else:
        await interaction.followup.send(f"❌ **제재 실패:** {display}(제재 실행 실패)")

@app_commands.describe(멤버="제재 복구 대상 멤버를 선택하세요.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def clear_punish_cb(interaction: discord.Interaction, 멤버: discord.Member):
    if not await require_setting_channel(interaction, "제재로그", "제재지우기"):
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    log_channel = guild.get_channel(GUILD_SETTINGS[guild.id]["punish_log_channel"])

    if 멤버.top_role >= guild.me.top_role:
        await interaction.followup.send("❌ 대상 유저의 서열이 봇보다 높거나 같아 제재를 감면할 수 없습니다.")
        return

    try:
        await 멤버.edit(timed_out_until=None, reason="관리자가 제재 스택 감면")
        current_count = 0
        roles_to_remove = []
        for r in 멤버.roles:
            if r.name.startswith("전과 ") and r.name.endswith("범"):
                try:
                    current_count = int(r.name.replace("전과 ", "").replace("범", "").strip())
                    roles_to_remove.append(r)
                except ValueError:
                    continue

        if roles_to_remove:
            await 멤버.remove_roles(*roles_to_remove)

        next_count = max(0, current_count - 1)
        action_description = "대상자의 타임아웃 해제 및 전과 누적 단계를 초기화했습니다."

        if next_count > 0:
            new_role_name = f"전과 {next_count}범"
            role = discord.utils.get(guild.roles, name=new_role_name)
            if not role:
                try:
                    role = await guild.create_role(name=new_role_name)
                except discord.HTTPException:
                    if log_channel:
                        await log_channel.send("❌ **전과 스택 조정 실패:** 서버의 역할 개수 한도가 초과되었습니다.")
                    await interaction.followup.send("❌ 서버 역할 개수 제한으로 인해 새로운 전과 역할을 만들지 못했습니다.")
                    return
            await 멤버.add_roles(role)
            action_description = f"타임아웃을 해제하고 전과 스택을 1단계 격하했습니다. (**전과 {current_count}범** ➡️ **{new_role_name}**)"

        if log_channel:
            embed = discord.Embed(title="🔓 유저 제재 감면 및 복구 리포트", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{멤버.mention} ({멤버.name})", inline=True)
            embed.add_field(name="🔨 실행 관리자", value=str(interaction.user.name), inline=True)
            embed.description = action_description
            await log_channel.send(embed=embed)

        await interaction.followup.send(f"🔓 {format_member_name(멤버)} 유저의 제재 스택 감면 집행을 완료했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 권한 등급(서열)이 멤버 권한보다 낮아 조치를 거부당했습니다.")
    except Exception as e:
        await interaction.followup.send(f"❌ 예외 에러 발생: {e}")

# ---------- 추방 = 영구 차단(밴) ----------
@app_commands.describe(멤버="추방(영구 차단)할 멤버를 선택하세요.", 사유="추방 사유를 입력하세요. (감사 로그와 제재 로그에 기록됩니다)")
@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def ban_member_cb(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 추방"):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    display = format_member_name(멤버)

    if 멤버.id == interaction.user.id:
        await interaction.followup.send("❌ 자기 자신은 추방할 수 없습니다.")
        return
    if 멤버.id == guild.me.id:
        await interaction.followup.send("❌ 봇 자신은 추방할 수 없습니다.")
        return
    if 멤버.id == guild.owner_id:
        await interaction.followup.send("❌ 서버 소유자는 추방할 수 없습니다.")
        return
    if 멤버.top_role >= guild.me.top_role:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n봇의 역할 순위가 대상보다 낮거나 같습니다. 서버 설정에서 봇 역할을 위로 올려주세요.")
        return
    if interaction.user.id != guild.owner_id and 멤버.top_role >= interaction.user.top_role:
        await interaction.followup.send("❌ 자신과 같거나 더 높은 역할을 가진 멤버는 추방할 수 없습니다.")
        return

    try:
        await guild.ban(멤버, reason=f"{사유} (집행자: {interaction.user.name})"[:512])
    except discord.Forbidden:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n봇에게 '멤버 차단하기' 권한이 없거나 역할 순위가 부족합니다.")
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n`{e}`")
        return

    safe_reason = discord.utils.escape_mentions(사유)
    await interaction.followup.send(f"🔨 **추방(영구 차단) 완료:** {display}\n📝 **사유:** {safe_reason}\n다시 들어오려면 `/추방해제` 가 필요합니다.")

    embed = discord.Embed(title="🔨 유저 추방(영구 차단) 알림", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="👤 대상자", value=f"{멤버.mention} ({멤버.name})", inline=True)
    embed.add_field(name="🔨 집행자", value=str(interaction.user.name), inline=True)
    embed.add_field(name="💬 추방 사유", value=safe_reason[:1024], inline=False)
    await send_punish_log(guild, embed)

# ---------- 추방해제: 차단 목록에서 선택 ----------
class UnbanView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, invoker_id: int, entries: list):
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.entries = entries
        self.page = 0
        self.interaction = None
        self._render()

    @property
    def last_page(self) -> int:
        return max(0, (len(self.entries) - 1) // self.PAGE_SIZE)

    def make_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔓 추방 해제",
            description=f"이 서버에서 차단된 유저 **{len(self.entries)}명**\n아래 목록에서 해제할 유저를 선택하세요.",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"{self.page + 1} / {self.last_page + 1} 페이지 · 선택하면 즉시 차단이 해제됩니다")
        return embed

    def _render(self):
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        chunk = self.entries[start:start + self.PAGE_SIZE]
        options = [
            discord.SelectOption(label=(e["label"][:100] or str(e["id"])), value=str(e["id"]), description=e["desc"][:100])
            for e in chunk
        ]
        select = discord.ui.Select(placeholder="🔓 해제할 유저를 선택하세요", options=options, row=0)
        select.callback = self.on_select
        self.add_item(select)
        if self.last_page > 0:
            prev_btn = discord.ui.Button(label="◀ 이전", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            next_btn = discord.ui.Button(label="다음 ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.last_page, row=1)
            prev_btn.callback = self.on_prev
            next_btn.callback = self.on_next
            self.add_item(prev_btn)
            self.add_item(next_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ 명령어를 입력한 본인만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.page = min(self.last_page, self.page + 1)
        self._render()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_select(self, interaction: discord.Interaction):
        uid = int(interaction.data["values"][0])
        entry = next((e for e in self.entries if e["id"] == uid), None)
        name = safe_text(entry["label"]) if entry else str(uid)
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=f"관리자 수동 차단 해제 (집행자: {interaction.user.name})"[:512])
        except discord.NotFound:
            await interaction.response.edit_message(content=f"⚠️ **{name}** 님은 이미 차단 목록에 없습니다.", embed=None, view=None)
            self.stop()
            return
        except discord.Forbidden:
            await interaction.response.edit_message(content="❌ 봇에게 '멤버 차단하기' 권한이 없어 해제할 수 없습니다.", embed=None, view=None)
            self.stop()
            return
        except discord.HTTPException as e:
            await interaction.response.edit_message(content=f"❌ 차단 해제 중 오류가 발생했습니다: `{e}`", embed=None, view=None)
            self.stop()
            return

        await interaction.response.edit_message(content=f"✅ **추방 해제 완료:** {name}\n이제 다시 서버에 들어올 수 있습니다.", embed=None, view=None)
        self.stop()

        embed = discord.Embed(title="🔓 유저 추방 해제 알림", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 대상자", value=f"{name} (ID: {uid})", inline=True)
        embed.add_field(name="🔨 집행자", value=str(interaction.user.name), inline=True)
        await send_punish_log(interaction.guild, embed)

    async def on_timeout(self):
        if self.interaction:
            try:
                await self.interaction.edit_original_response(content="⏰ 시간이 초과되었습니다. `/추방해제` 를 다시 입력해 주세요.", embed=None, view=None)
            except discord.HTTPException:
                pass

@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def unban_member_cb(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    try:
        bans = [entry async for entry in guild.bans(limit=None)]
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 '멤버 차단하기' 권한이 없어 차단 목록을 불러올 수 없습니다.")
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 차단 목록을 불러오지 못했습니다: `{e}`")
        return

    if not bans:
        await interaction.followup.send("✅ 이 서버에는 차단된 유저가 없습니다.")
        return

    entries = []
    for be in bans:
        u = be.user
        shown = u.global_name or u.name
        label = f"{shown} ({u.name})" if shown != u.name else u.name
        reason = (be.reason or "사유 없음").replace("\n", " ")
        entries.append({"id": u.id, "label": label, "desc": f"ID {u.id} · {reason}"})
    entries.sort(key=lambda e: e["label"].lower())

    view = UnbanView(interaction.user.id, entries)
    await interaction.followup.send(embed=view.make_embed(), view=view)
    view.interaction = interaction

# ---------- 청소 / 서버청소 ----------
RUNNING_CLEANS = set()   # 서버별 동시 청소 방지

@app_commands.default_permissions(manage_messages=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def clean_channel_cb(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
        await interaction.response.send_message("❌ 이 채널에서는 청소를 사용할 수 없습니다.", ephemeral=True)
        return
    perms = channel.permissions_for(guild.me)
    if not (perms.manage_messages and perms.read_message_history):
        await interaction.response.send_message("❌ 봇에게 '메시지 관리'와 '메시지 기록 보기' 권한이 필요합니다.", ephemeral=True)
        return
    if guild.id in RUNNING_CLEANS:
        await interaction.response.send_message("⏳ 이 서버에서 이미 청소가 진행 중입니다. 끝난 뒤 다시 시도해 주세요.", ephemeral=True)
        return

    warn_text = (
        f"🧹 **경고: 채널 전체 청소**\n"
        f"{interaction.user.mention}님, {channel.mention} 의 **모든 메세지(고정 메세지 포함)** 가 삭제되며 되돌릴 수 없습니다.\n"
        f"메세지가 많거나 오래됐다면 시간이 오래 걸릴 수 있습니다.\n\n"
        f"✅ 를 누르면 **청소 진행** / ❌ 를 누르면 **취소**\n"
        f"(명령어를 입력한 본인만 선택할 수 있으며, {CONFIRM_TIMEOUT}초 안에 선택하지 않으면 자동 취소됩니다.)"
    )
    if not await confirm_by_reaction(interaction, warn_text, "채널 청소"):
        return

    RUNNING_CLEANS.add(guild.id)
    result = None
    try:
        deleted = await channel.purge(limit=None, bulk=True, reason=f"/청소 (집행자: {interaction.user.name})")
        result = f"🧹 {channel.mention} 의 메세지 **{len(deleted)}개**를 삭제했습니다."
    except discord.Forbidden:
        result = "❌ 권한이 부족해 청소를 끝까지 진행하지 못했습니다."
    except discord.HTTPException as e:
        result = f"❌ 청소 중 오류가 발생했습니다: `{e}`"
    finally:
        RUNNING_CLEANS.discard(guild.id)

    try:
        await interaction.followup.send(result, ephemeral=True)
    except discord.HTTPException:
        try:
            await channel.send(result, delete_after=10)
        except discord.HTTPException:
            pass

async def collect_all_threads(guild: discord.Guild) -> dict:
    """활성 + 보관된 스레드와 포럼 게시글을 모두 모읍니다."""
    found = {}
    for t in guild.threads:
        found[t.id] = t
    try:
        for t in await guild.active_threads():
            found[t.id] = t
    except discord.HTTPException:
        pass
    for ch in guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            perms = ch.permissions_for(guild.me)
            if not (perms.view_channel and perms.read_message_history):
                continue
            try:
                async for t in ch.archived_threads(limit=None):
                    found[t.id] = t
                if isinstance(ch, discord.TextChannel) and perms.manage_threads:
                    async for t in ch.archived_threads(limit=None, private=True):
                        found[t.id] = t
            except discord.HTTPException:
                continue
    return found

@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def clean_server_cb(interaction: discord.Interaction):
    guild = interaction.guild
    if guild.id in RUNNING_CLEANS:
        await interaction.response.send_message("⏳ 이 서버에서 이미 청소가 진행 중입니다. 끝난 뒤 다시 시도해 주세요.", ephemeral=True)
        return

    warn_text = (
        f"🧨 **경고: 서버 전체 청소**\n"
        f"{interaction.user.mention}님, 이 서버의 **모든 채널 메세지, 스레드, 포럼 게시글(포스트)** 이 삭제되며 **절대 되돌릴 수 없습니다.**\n"
        f"(채널과 역할은 삭제되지 않습니다. 양이 많으면 오랜 시간이 걸립니다.)\n\n"
        f"✅ 를 누르면 **서버 청소 진행** / ❌ 를 누르면 **취소**\n"
        f"(명령어를 입력한 본인만 선택할 수 있으며, {CONFIRM_TIMEOUT}초 안에 선택하지 않으면 자동 취소됩니다.)"
    )
    if not await confirm_by_reaction(interaction, warn_text, "서버 청소"):
        return

    RUNNING_CLEANS.add(guild.id)
    stats = {"messages": 0, "threads": 0, "skipped": 0, "failed": 0}
    try:
        try:
            await interaction.followup.send("🧨 서버 청소를 시작했습니다. 끝나면 결과를 알려드릴게요. (시간이 걸릴 수 있어요)", ephemeral=True)
        except discord.HTTPException:
            pass

        # 1) 스레드 · 포럼 게시글 삭제 (스레드 안의 메세지도 함께 사라집니다)
        threads = await collect_all_threads(guild)
        for t in threads.values():
            parent = t.parent
            if parent is None or not parent.permissions_for(guild.me).manage_threads:
                stats["skipped"] += 1
                continue
            try:
                await t.delete(reason=f"/서버청소 (집행자: {interaction.user.name})")
                stats["threads"] += 1
            except discord.HTTPException:
                stats["failed"] += 1

        # 2) 채널 메세지 삭제
        for ch in guild.channels:
            if not isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
                continue
            p = ch.permissions_for(guild.me)
            if not (p.view_channel and p.manage_messages and p.read_message_history):
                stats["skipped"] += 1
                continue
            try:
                deleted = await ch.purge(limit=None, bulk=True, reason=f"/서버청소 (집행자: {interaction.user.name})")
                stats["messages"] += len(deleted)
            except discord.HTTPException:
                stats["failed"] += 1
    finally:
        RUNNING_CLEANS.discard(guild.id)

    embed = discord.Embed(title="🧨 서버 청소 완료", color=discord.Color.dark_red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="💬 삭제한 메세지", value=f"{stats['messages']}개", inline=True)
    embed.add_field(name="🧵 삭제한 스레드·포스트", value=f"{stats['threads']}개", inline=True)
    embed.add_field(name="⏭️ 권한 부족으로 건너뜀", value=f"{stats['skipped']}곳", inline=True)
    if stats["failed"]:
        embed.add_field(name="⚠️ 오류로 실패", value=f"{stats['failed']}곳", inline=True)
    embed.set_footer(text="건너뛴 곳은 봇에 '채널 보기·메시지 관리·메시지 기록 보기·스레드 관리' 권한을 주면 청소됩니다.")
    try:
        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.HTTPException:
        try:
            await interaction.user.send(embed=embed)
        except discord.HTTPException:
            pass

# ---------- 가이드 ----------
CATEGORY_WARN = {
    "인증": "`인증방`·`인증지급`·`인증메세지` 중 하나라도 비어 있으면 신입에게 안내가 안 뜨거나 역할을 못 줘서 **가입 단계가 멈춥니다.** (`인증방`이 없으면 `/인증완료`도 실행되지 않아요.)",
    "제재": "`제재로그`가 비어 있으면 **`/제재`·`/제재지우기`를 쓸 수 없고**, 자동 제재는 처리돼도 로그가 남지 않습니다.",
    "입퇴장": "로그 채널을 정하지 않으면 해당 입장/퇴장 기록이 남지 않습니다.",
    "청소": "삭제는 **되돌릴 수 없습니다.** 반드시 ✅/❌ 확인을 거치며, 봇에 '메시지 관리' 권한이 필요합니다.",
    "기타": "",
}

def build_guide_embed(guild_id: int, page: str) -> discord.Embed:
    enabled = enabled_keys(guild_id)
    embed = discord.Embed(color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.set_footer(text="안전하고 청정한 서버 환경을 구축합니다 • Powered by NattoBot")

    if page == "home":
        embed.title = "🛡️ 낫또봇 가이드"
        embed.description = "필요한 기능만 골라 켜서 쓰는 방식이에요. 딱 3단계면 끝!"
        embed.add_field(name="1️⃣ 기능 켜기", value="`/활성화` → 카테고리 → 기능 선택 → Enter\n카테고리의 `전체`를 고르면 한 번에 켤 수 있어요.", inline=False)
        embed.add_field(name="2️⃣ 값 설정하기", value="`/설정` → 카테고리 → 기능 → 값 입력\n예) `/설정 인증 인증지급 역할:@멤버`", inline=False)
        embed.add_field(name="3️⃣ 관리하기", value="`/체크` 확인 · `/제거` 일부 삭제 · `/초기화` 전체 비우기\n기능을 끄려면 `/비활성화`, 전체 활성화는 `/모두활성화`, 전체 끄기는 `/모두비활성화`", inline=False)
        lines = []
        for cat, (emoji, _) in CATEGORIES.items():
            keys = [k for k, f in FEATURES.items() if f["cat"] == cat]
            on = sum(1 for k in keys if k in enabled)
            lines.append(f"{emoji} **『{cat}』** `{'▰' * on}{'▱' * (len(keys) - on)}` {on}/{len(keys)}")
        embed.add_field(name="📊 현재 활성화 현황", value="\n".join(lines), inline=False)
        embed.set_author(name="아래 메뉴에서 카테고리를 골라 자세히 보세요 👇")
        return embed

    if page == "common":
        embed.title = "🧰 공통 명령어"
        embed.description = "켜 둔 기능만 목록에 나타납니다."
        embed.add_field(name="⚙️ /설정 [카테고리] [기능]", value="값을 등록합니다. 카테고리로 나뉘어 있어 빨리 찾을 수 있어요.", inline=False)
        embed.add_field(name="📋 /체크 [기능]", value="현재 설정된 값을 확인합니다.", inline=False)
        embed.add_field(name="🗑️ /제거 [기능]", value="설정을 지우거나, 목록에서 하나만 골라 뺍니다.", inline=False)
        embed.add_field(name="♻️ /초기화 [기능]", value="목록을 전부 비웁니다. 경고 메시지의 ✅ 를 눌러야 실행되고 ❌ 는 취소예요.", inline=False)
        embed.add_field(name="⛔ /비활성화 [기능]", value="기능을 끕니다. 저장된 설정값은 남아 있어서 다시 켜면 그대로 쓸 수 있어요.", inline=False)
        return embed

    emoji, desc = CATEGORIES[page]
    embed.title = f"{emoji} 『{page}』 카테고리"
    embed.description = desc
    for k, f in FEATURES.items():
        if f["cat"] != page:
            continue
        status = "✅" if k in enabled else "⬜"
        cmds = " · ".join(f"`{c}`" for c in available_cmds(k))
        usage = f" {f['usage']}" if f.get("usage") else ""
        embed.add_field(
            name=f"{status} {k}",
            value=f"{f['desc']}\n└ {cmds}{usage}" + ("" if k in enabled else f"\n└ 켜기: `/활성화 {page} {k}`"),
            inline=False
        )
    warn = CATEGORY_WARN.get(page)
    if warn:
        embed.add_field(name="🚨 주의", value=warn, inline=False)
    return embed

class GuideView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.interaction = None
        options = [discord.SelectOption(label="시작하기", value="home", emoji="🏠", description="처음 쓰는 방법과 활성화 현황")]
        for cat, (emoji, desc) in CATEGORIES.items():
            options.append(discord.SelectOption(label=cat, value=cat, emoji=emoji, description=desc[:100]))
        options.append(discord.SelectOption(label="공통 명령어", value="common", emoji="🧰", description="설정·체크·제거·초기화 사용법"))
        select = discord.ui.Select(placeholder="📚 보고 싶은 항목을 선택하세요", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        page = interaction.data["values"][0]
        await interaction.response.edit_message(embed=build_guide_embed(self.guild_id, page), view=self)

    async def on_timeout(self):
        if self.interaction:
            try:
                await self.interaction.edit_original_response(view=None)
            except discord.HTTPException:
                pass

@app_commands.guild_only()
async def guide_cb(interaction: discord.Interaction):
    view = GuideView(interaction.guild.id)
    await interaction.response.send_message(embed=build_guide_embed(interaction.guild.id, "home"), view=view, ephemeral=True)
    view.interaction = interaction

# ====================================================================
# 📚 기능 카탈로그 (카테고리 · 명령어 매핑)
# ====================================================================
CATEGORIES = {
    "인증": ("🪪", "신입 유저 인증과 역할 지급"),
    "제재": ("🚨", "제재·검열·추방 관리"),
    "입퇴장": ("🚪", "유저 입장·퇴장 로그"),
    "청소": ("🧹", "메세지·스레드 청소"),
    "기타": ("📖", "사용법 안내"),
}

FEATURES = {
    # ---------- 인증 ----------
    "인증방": {
        "cat": "인증", "label": "인증 채널", "store": "auth_channel",
        "desc": "신입이 들어오면 인증 안내가 올라올 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("auth_channel", "인증 채널"),
        "remove": make_remove_channel("auth_channel", "인증 채널"),
        "check": make_check_channel("auth_channel", "인증 채널"),
    },
    "인증지급": {
        "cat": "인증", "label": "인증지급 역할", "store": "auth_role_ids",
        "desc": "인증 완료 시 지급할 기본 역할을 등록합니다. (여러 개 가능)", "usage": "`역할`",
        "descs": {"set": "인증 완료 시 지급할 기본 역할을 추가 등록합니다.", "remove": "인증지급 역할 중 선택한 하나만 제외합니다."},
        "set": set_auth_role_cb, "remove": remove_auth_role_cb, "check": check_auth_role_cb,
        "reset": make_reset("auth_role_ids", "인증지급 역할"),
    },
    "인증메세지": {
        "cat": "인증", "label": "인증 안내 메세지", "store": "auth_form",
        "desc": "신입 입장 시 인증방에 보낼 안내 문구를 등록합니다.", "usage": "`메세지`",
        "set": set_auth_form_cb, "check": check_auth_form_cb,
    },
    "인증완료": {
        "cat": "인증", "desc": "대기자에게 기본 역할과 추가 역할을 지급해 인증을 승인합니다.", "usage": "`유저` `추가역할`",
        "top": approve_user_cb,
    },
    # ---------- 제재 ----------
    "제재로그": {
        "cat": "제재", "label": "제재 로그 채널", "store": "punish_log_channel",
        "desc": "제재·전과·추방 내역이 기록될 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("punish_log_channel", "제재 로그 채널"),
        "remove": make_remove_channel("punish_log_channel", "제재 로그 채널"),
        "check": make_check_channel("punish_log_channel", "제재 로그 채널"),
    },
    "제재단어": {
        "cat": "제재", "label": "제재 단어", "store": "ban_words",
        "desc": "포함되면 메세지 삭제 + 자동 제재할 단어를 등록합니다.", "usage": "`단어`",
        "descs": {"set": "자동 제재할 단어를 등록합니다. 쉼표(,)로 여러 개 가능.", "remove": "등록된 제재 단어 중 선택한 하나만 삭제합니다."},
        "set": make_set_words("ban_words", "제재 단어", "🚨 **자동 제재 단어 추가 완료:**"),
        "remove": make_remove_word("ban_words", "제재 단어"),
        "check": make_check_words("ban_words", "제재 단어", "🚨"),
        "reset": make_reset("ban_words", "제재 단어"),
    },
    "검열단어": {
        "cat": "제재", "label": "검열 단어", "store": "censor_words",
        "desc": "포함되면 메세지를 스포일러로 바꿀 단어를 등록합니다.", "usage": "`단어`",
        "descs": {"set": "스포일러 처리할 단어를 등록합니다. 쉼표(,)로 여러 개 가능.", "remove": "등록된 검열 단어 중 선택한 하나만 삭제합니다."},
        "set": make_set_words("censor_words", "검열 단어", "🔒 **스포일러 검열 단어 추가 완료:**"),
        "remove": make_remove_word("censor_words", "검열 단어"),
        "check": make_check_words("censor_words", "검열 단어", "🔒"),
        "reset": make_reset("censor_words", "검열 단어"),
    },
    "제재": {
        "cat": "제재", "desc": "멤버를 수동 제재합니다. (타임아웃 + 전과 역할 부여)", "usage": "`멤버` `사유`",
        "top": manual_punish_cb,
    },
    "제재지우기": {
        "cat": "제재", "desc": "타임아웃을 풀고 전과 단계를 1범 감면합니다.", "usage": "`멤버`",
        "top": clear_punish_cb,
    },
    "추방": {
        "cat": "제재", "desc": "멤버를 서버에서 추방하고 영구 차단합니다. (다시 못 들어옴)", "usage": "`멤버` `사유`",
        "top": ban_member_cb,
    },
    "추방해제": {
        "cat": "제재", "desc": "차단된 유저 목록을 보여주고 선택한 유저의 차단을 풉니다.", "usage": "(입력 후 목록에서 선택)",
        "top": unban_member_cb,
    },
    # ---------- 입퇴장 ----------
    "입장로그": {
        "cat": "입퇴장", "label": "입장 로그 채널", "store": "join_log_channel",
        "desc": "유저가 서버에 들어왔을 때 기록을 남길 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("join_log_channel", "입장 로그 채널"),
        "remove": make_remove_channel("join_log_channel", "입장 로그 채널"),
        "check": make_check_channel("join_log_channel", "입장 로그 채널"),
    },
    "퇴장로그": {
        "cat": "입퇴장", "label": "퇴장 로그 채널", "store": "leave_log_channel",
        "desc": "유저가 서버를 나갔을 때 기록을 남길 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("leave_log_channel", "퇴장 로그 채널"),
        "remove": make_remove_channel("leave_log_channel", "퇴장 로그 채널"),
        "check": make_check_channel("leave_log_channel", "퇴장 로그 채널"),
    },
    # ---------- 청소 ----------
    "청소": {
        "cat": "청소", "desc": "명령어를 쓴 채널의 모든 메세지를 삭제합니다. (✅/❌ 확인)", "usage": "",
        "top": clean_channel_cb,
    },
    "서버청소": {
        "cat": "청소", "desc": "서버의 모든 메세지·스레드·포스트를 삭제합니다. (관리자, ✅/❌ 확인)", "usage": "",
        "top": clean_server_cb,
    },
    # ---------- 기타 ----------
    "가이드": {
        "cat": "기타", "desc": "낫또봇 사용법을 카테고리별로 보여줍니다.", "usage": "",
        "top": guide_cb,
    },
}

VERB_INFO = {
    "설정": ("set", "활성화한 기능의 값을 설정합니다. (관리자 전용)"),
    "제거": ("remove", "활성화한 기능의 설정을 제거합니다. (관리자 전용)"),
    "체크": ("check", "활성화한 기능의 현재 설정을 확인합니다. (관리자 전용)"),
    "초기화": ("reset", "활성화한 기능의 목록을 모두 비웁니다. (관리자 전용)"),
}

def available_cmds(key: str) -> list:
    f = FEATURES[key]
    if "top" in f:
        return [f"/{key}"]
    out = []
    if "set" in f:
        out.append(f"/설정 『{f['cat']}』 {key}")
    if "check" in f:
        out.append(f"/체크 {key}")
    if "remove" in f:
        out.append(f"/제거 {key}")
    if "reset" in f:
        out.append(f"/초기화 {key}")
    return out

def leaf_description(verb: str, field: str, f: dict) -> str:
    custom = f.get("descs", {}).get(field)
    if custom:
        return custom[:100]
    label = f["label"]
    return {
        "설정": f"{label}{josa(label, '을', '를')} 설정합니다.",
        "제거": f"{label} 설정을 제거합니다.",
        "체크": f"현재 {label} 설정을 확인합니다.",
        "초기화": f"등록된 {label}{josa(label, '을', '를')} 모두 초기화합니다. (✅/❌ 확인)",
    }[verb][:100]

# ====================================================================
# 🧩 활성화 시스템 (서버별 명령어 동적 등록)
# ====================================================================
_GUILD_LOCKS = {}

def get_lock(guild_id: int) -> asyncio.Lock:
    return _GUILD_LOCKS.setdefault(guild_id, asyncio.Lock())


async def feature_autocomplete(interaction: discord.Interaction, current: str):
    enabled = enabled_keys(interaction.guild.id) if interaction.guild else set()
    cur = current.lower()
    return [app_commands.Choice(name=f"『{FEATURES[k]['cat']}』 · {k}"[:100], value=k)
            for k in FEATURES if k in enabled and cur in k.lower()][:25]

async def all_feature_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    return [app_commands.Choice(name=f"『{FEATURES[k]['cat']}』 · {k}"[:100], value=k)
            for k in FEATURES if cur in k.lower()][:25]

@app_commands.describe(기능="설정할 기능을 선택하세요.", 값="채널은 #채널/ID, 역할은 @역할/ID, 문구나 단어는 내용을 입력하세요.")
@app_commands.autocomplete(기능=feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_set_cb(interaction: discord.Interaction, 기능: str, 값: str):
    if 기능 not in enabled_keys(interaction.guild.id):
        await interaction.response.send_message("❌ 먼저 `/활성화`에서 해당 기능을 활성화해 주세요.", ephemeral=True); return
    gs = get_gs(interaction.guild.id)
    import re
    if 기능 in ("인증방", "제재로그", "입장로그", "퇴장로그"):
        f = FEATURES[기능]; m = re.search(r"\d{15,25}", 값)
        channel = interaction.guild.get_channel(int(m.group())) if m else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("❌ 유효한 텍스트 채널을 `#채널` 또는 채널 ID로 입력해 주세요.", ephemeral=True); return
        gs[f["store"]] = channel.id; result = f"✅ **{기능}**을 {channel.mention}(으)로 설정했습니다."
    elif 기능 == "인증지급":
        m = re.search(r"\d{15,25}", 값); role = interaction.guild.get_role(int(m.group())) if m else None
        if not role:
            await interaction.response.send_message("❌ 유효한 역할을 `@역할` 또는 역할 ID로 입력해 주세요.", ephemeral=True); return
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message("❌ 봇보다 높거나 같은 역할은 등록할 수 없습니다.", ephemeral=True); return
        ids = gs.setdefault("auth_role_ids", [])
        if role.id not in ids: ids.append(role.id)
        result = f"✅ 인증 지급 역할에 {role.mention}을(를) 등록했습니다."
    elif 기능 == "인증메세지":
        gs["auth_form"] = 값; result = "✅ 인증 안내 메세지를 저장했습니다."
    elif 기능 in ("제재단어", "검열단어"):
        key = "ban_words" if 기능 == "제재단어" else "censor_words"
        words = gs.setdefault(key, [])
        added = [w.strip() for w in 값.split(",") if w.strip() and w.strip() not in words]
        if not added:
            await interaction.response.send_message("⚠️ 새로 등록할 단어가 없습니다.", ephemeral=True); return
        words.extend(added); result = f"✅ {기능} 등록 완료: {', '.join(added)}"
    else:
        await interaction.response.send_message("❌ 이 항목은 값 설정을 지원하지 않습니다.", ephemeral=True); return
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(result, ephemeral=True)

@app_commands.describe(기능="확인할 기능을 선택하세요.")
@app_commands.autocomplete(기능=feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_check_cb(interaction: discord.Interaction, 기능: str):
    f = FEATURES.get(기능, {}); gs = GUILD_SETTINGS.get(interaction.guild.id, {})
    store = f.get("store")
    if 기능 in ("인증방", "제재로그", "입장로그", "퇴장로그"):
        ch = interaction.guild.get_channel(gs.get(store, 0)); value = ch.mention if ch else "설정되지 않음"
    elif 기능 == "인증지급":
        value = ", ".join(r.mention for r in (interaction.guild.get_role(x) for x in gs.get("auth_role_ids", [])) if r) or "설정되지 않음"
    elif 기능 == "인증메세지": value = gs.get("auth_form", "설정되지 않음").replace("\\\\n", "\\n")
    elif 기능 in ("제재단어", "검열단어"):
        value = ", ".join(gs.get("ban_words" if 기능 == "제재단어" else "censor_words", [])) or "등록된 단어 없음"
    else: value = "이 기능은 별도 설정값이 없습니다."
    await interaction.response.send_message(f"📋 **{기능} 현재 설정**\\n{value[:1800]}", ephemeral=True)

@app_commands.describe(기능="제거할 설정 항목을 선택하세요.", 값="단어 목록에서 특정 단어를 제거할 때 입력하세요. 비우면 목록 전체를 비웁니다.")
@app_commands.autocomplete(기능=feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_remove_cb(interaction: discord.Interaction, 기능: str, 값: str = ""):
    f = FEATURES.get(기능, {}); gs = GUILD_SETTINGS.get(interaction.guild.id, {}); store = f.get("store")
    if 기능 in ("제재단어", "검열단어"):
        key = "ban_words" if 기능 == "제재단어" else "censor_words"; words = gs.get(key, [])
        if 값:
            if 값 not in words:
                await interaction.response.send_message("❌ 해당 단어가 등록되어 있지 않습니다.", ephemeral=True); return
            words.remove(값); result = f"🗑️ `{값}` 단어를 제거했습니다."
        else: gs[key] = []; result = f"🗑️ {기능} 목록을 비웠습니다."
    elif 기능 == "인증지급": gs["auth_role_ids"] = []; result = "🗑️ 인증지급 역할 목록을 비웠습니다."
    elif store and store in gs: del gs[store]; result = f"🗑️ {기능} 설정을 제거했습니다."
    else:
        await interaction.response.send_message("❌ 제거할 설정이 없습니다.", ephemeral=True); return
    save_settings(GUILD_SETTINGS); await interaction.response.send_message(result, ephemeral=True)

@app_commands.describe(기능="초기화할 목록 기능을 선택하세요.")
@app_commands.autocomplete(기능=feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_reset_cb(interaction: discord.Interaction, 기능: str):
    gs = GUILD_SETTINGS.get(interaction.guild.id, {})
    key = {"인증지급":"auth_role_ids", "제재단어":"ban_words", "검열단어":"censor_words"}.get(기능)
    if not key:
        await interaction.response.send_message("❌ 이 기능은 목록 초기화를 지원하지 않습니다. `/제거`를 이용해 주세요.", ephemeral=True); return
    old = gs.get(key, [])
    if not old:
        await interaction.response.send_message("⚠️ 초기화할 항목이 없습니다.", ephemeral=True); return
    gs[key] = []; save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"♻️ **{기능}** 목록의 {len(old)}개 항목을 초기화했습니다.", ephemeral=True)

@app_commands.describe(기능="활성화할 기능을 선택하세요.")
@app_commands.autocomplete(기능=all_feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_activate_cb(interaction: discord.Interaction, 기능: str):
    if 기능 not in FEATURES:
        await interaction.response.send_message("❌ 목록에서 기능을 선택해 주세요.", ephemeral=True); return
    await activate_features(interaction, [기능])

@app_commands.describe(대상="역할을 지급할 멤버", 역할="지급할 역할")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
async def give_role_cb(interaction: discord.Interaction, 대상: discord.Member, 역할: discord.Role):
    if 역할 >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ 봇보다 높거나 같은 역할은 지급할 수 없습니다.", ephemeral=True); return
    try:
        await 대상.add_roles(역할, reason=f"/역할주기 실행자: {interaction.user}")
        await interaction.response.send_message(f"✅ {대상.mention}님에게 {역할.mention} 역할을 지급했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 봇의 역할 순위 또는 권한이 부족합니다.", ephemeral=True)

@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@admin_only
async def activate_all_cb(interaction: discord.Interaction):
    await activate_features(interaction, list(FEATURES.keys()))

@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@admin_only
async def deactivate_all_cb(interaction: discord.Interaction):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)
    async with get_lock(guild.id):
        gs = get_gs(guild.id)
        old = list(gs.get("enabled", []))
        if not old:
            await interaction.followup.send("⚠️ 현재 활성화된 기능이 없습니다.")
            return
        gs["enabled"] = []
        save_settings(GUILD_SETTINGS)
        try:
            await refresh_guild_commands(guild)
        except discord.HTTPException as e:
            gs["enabled"] = old
            save_settings(GUILD_SETTINGS)
            register_guild_commands(guild.id)
            await interaction.followup.send(f"❌ 명령어 갱신에 실패했습니다. 잠시 후 다시 시도해 주세요.\\n`{e}`")
            return
    await interaction.followup.send(f"⛔ 전체 기능 {len(old)}개를 비활성화했습니다. 저장된 설정값은 유지됩니다.")

def build_guild_commands(guild_id: int) -> list:
    enabled = enabled_keys(guild_id)
    if not enabled:
        return [
            app_commands.Command(name="모두활성화", description="모든 기능을 한 번에 활성화합니다.", callback=activate_all_cb),
            app_commands.Command(name="모두비활성화", description="모든 기능을 한 번에 비활성화합니다.", callback=deactivate_all_cb),
        ]
    result = [
        app_commands.Command(name="설정", description="기능을 선택해 값을 설정합니다.", callback=generic_set_cb),
        app_commands.Command(name="체크", description="기능의 현재 설정을 확인합니다.", callback=generic_check_cb),
        app_commands.Command(name="제거", description="기능의 설정 또는 목록 항목을 제거합니다.", callback=generic_remove_cb),
        app_commands.Command(name="초기화", description="기능의 목록을 초기화합니다.", callback=generic_reset_cb),
        app_commands.Command(name="비활성화", description="켜 둔 기능 하나를 끕니다.", callback=deactivate_cb),
        app_commands.Command(name="모두활성화", description="모든 기능을 한 번에 활성화합니다.", callback=activate_all_cb),
        app_commands.Command(name="모두비활성화", description="모든 기능을 한 번에 비활성화합니다.", callback=deactivate_all_cb),
        app_commands.Command(name="역할주기", description="선택한 멤버에게 역할을 지급합니다.", callback=give_role_cb),
    ]
    for k, f in FEATURES.items():
        if k in enabled and "top" in f:
            result.append(app_commands.Command(name=k, description=f["desc"][:100], callback=f["top"]))
    return result

def register_guild_commands(guild_id: int) -> discord.Object:
    """로컬 명령어 트리만 갱신합니다. (디스코드 서버 동기화는 하지 않음)"""
    obj = discord.Object(id=guild_id)
    bot.tree.clear_commands(guild=obj)
    for c in build_guild_commands(guild_id):
        bot.tree.add_command(c, guild=obj, override=True)
    return obj

async def refresh_guild_commands(guild: discord.Guild):
    obj = register_guild_commands(guild.id)
    await bot.tree.sync(guild=obj)

def activation_embed(keys: list) -> discord.Embed:
    embed = discord.Embed(title="✅ 활성화 완료", description="아래 명령어를 이제 사용할 수 있어요!", color=discord.Color.green())
    for k in keys:
        f = FEATURES[k]
        cmds = " · ".join(f"`{c}`" for c in available_cmds(k))
        embed.add_field(name=f"{CATEGORIES[f['cat']][0]} 『{f['cat']}』 · {k}", value=f"{f['desc']}\n└ {cmds}", inline=False)
    embed.set_footer(text="명령어가 안 보이면 몇 초 기다렸다가 디스코드를 Ctrl+R 로 새로고침해 보세요.")
    return embed

async def activate_features(interaction: discord.Interaction, keys: list):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)
    async with get_lock(guild.id):
        gs = get_gs(guild.id)
        old = list(gs.get("enabled", []))
        new = [k for k in keys if k not in old]
        if not new:
            await interaction.followup.send("⚠️ 선택한 기능은 이미 모두 활성화되어 있습니다.")
            return
        gs["enabled"] = [k for k in FEATURES if k in old or k in new]
        save_settings(GUILD_SETTINGS)
        try:
            await refresh_guild_commands(guild)
        except discord.HTTPException as e:
            gs["enabled"] = old
            save_settings(GUILD_SETTINGS)
            register_guild_commands(guild.id)
            await interaction.followup.send(f"❌ 명령어 등록에 실패했습니다. 잠시 후 다시 시도해 주세요.\n`{e}`")
            return
    await interaction.followup.send(embed=activation_embed(new))

def make_activate_cb(keys: list):
    @admin_only
    async def cb(interaction: discord.Interaction):
        await activate_features(interaction, keys)
    return cb

async def deactivate_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    cur = current.lower()
    choices = []
    for k in FEATURES:
        if k in enabled_keys(interaction.guild.id) and cur in k.lower():
            choices.append(app_commands.Choice(name=f"『{FEATURES[k]['cat']}』 · {k}", value=k))
    return choices[:25]

@app_commands.describe(기능="비활성화할 기능을 목록에서 선택하세요.")
@app_commands.autocomplete(기능=deactivate_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def deactivate_cb(interaction: discord.Interaction, 기능: str):
    guild = interaction.guild
    if 기능 not in enabled_keys(guild.id):
        await interaction.response.send_message("❌ 활성화되어 있지 않은 기능입니다. 목록에서 선택해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with get_lock(guild.id):
        gs = get_gs(guild.id)
        old = list(gs.get("enabled", []))
        gs["enabled"] = [k for k in old if k != 기능]
        save_settings(GUILD_SETTINGS)
        try:
            await refresh_guild_commands(guild)
        except discord.HTTPException as e:
            gs["enabled"] = old
            save_settings(GUILD_SETTINGS)
            register_guild_commands(guild.id)
            await interaction.followup.send(f"❌ 명령어 갱신에 실패했습니다. 잠시 후 다시 시도해 주세요.\n`{e}`")
            return
    await interaction.followup.send(f"⛔ **{기능}** 기능을 비활성화했습니다. 저장된 설정값은 남아 있으니 `/활성화 {FEATURES[기능]['cat']} {기능}` 로 다시 켜면 그대로 쓸 수 있어요.")

def build_activation_group() -> app_commands.Command:
    return app_commands.Command(name="활성화", description="사용할 기능을 검색해 하나씩 활성화합니다.", callback=generic_activate_cb)

# ================= [ 실행 ] =================
if __name__ == "__main__":
    import time
    keep_alive()
    time.sleep(10)  # 재시작 직후 연속 로그인으로 인한 레이트 리밋 방지
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except discord.HTTPException as e:
        print(f"⚠️ 로그인 실패 (status={e.status}). 10분 대기 후 종료합니다.")
        time.sleep(600)
        raise
