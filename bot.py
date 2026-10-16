import discord
from discord.ext import commands
import datetime
import asyncio
from flask import Flask
from threading import Thread
import os
import json
import tempfile
from discord import app_commands

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
DATA_FILE = "guild_settings.json"

def load_settings():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    except Exception as e:
        print(f"❌ 데이터 로드 중 오류 발생 (초기화 진행): {e}")
        return {}

def save_settings(settings):
    try:
        data_to_save = {str(k): v for k, v in settings.items()}
        dir_name = os.path.dirname(DATA_FILE) or '.'
        with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
            json.dump(data_to_save, tf, ensure_ascii=False, indent=4)
            temp_name = tf.name
        os.replace(temp_name, DATA_FILE)
    except Exception as e:
        print(f"❌ 데이터 저장 중 치명적 오류 발생: {e}")

GUILD_SETTINGS = load_settings()

intents = discord.Intents.default()
intents.message_content = True  
intents.members = True          

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= [ 멤버 표시 이름 헬퍼 ] =================
# 서버 닉네임을 적고, 괄호 안에 본 닉네임(디스코드 표시 이름, 없으면 계정 ID명)을 적습니다.
# 서버 닉네임이 따로 없어 두 이름이 같으면 중복 표기 없이 한 번만 적습니다.
def format_member_name(member: discord.Member) -> str:
    server_nick = member.display_name
    original_nick = member.global_name or member.name
    def clean(text: str) -> str:
        return discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    if server_nick == original_nick:
        return clean(server_nick)
    return f"{clean(server_nick)} ({clean(original_nick)})"

# ================= [ 필수 설정 검사 헬퍼 ] =================
def josa(word: str, with_final: str, without_final: str) -> str:
    """마지막 글자의 받침 유무에 따라 조사를 선택합니다. (예: 제재 → 를, 인증 승인 → 을)"""
    last = word.strip()[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 != 0 else without_final
    return without_final

async def require_setting_channel(interaction: discord.Interaction, key: str, label: str, setup_cmd: str, action: str) -> bool:
    """필수 채널 설정이 없으면 안내 문구를 남기고 False를 반환합니다."""
    guild = interaction.guild
    channel_id = GUILD_SETTINGS.get(guild.id, {}).get(key)
    if not channel_id:
        await interaction.response.send_message(
            f"⚠️ **{label}**{josa(label, '이', '가')} 설정되어 있지 않아 {action}{josa(action, '을', '를')} 실행할 수 없습니다.\n먼저 `{setup_cmd}` 명령어로 설정해 주세요.",
            ephemeral=True
        )
        return False
    if guild.get_channel(channel_id) is None:
        await interaction.response.send_message(
            f"⚠️ 설정된 **{label}**{josa(label, '을', '를')} 찾을 수 없습니다. (채널이 삭제되었거나 봇이 볼 수 없는 채널입니다)\n`{setup_cmd}` 명령어로 다시 설정해 주세요.",
            ephemeral=True
        )
        return False
    return True

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
# 자동 제재와 수동 제재가 모두 이 함수를 통해 "타임아웃 + 전과 역할 부여"를 처리합니다.
# 로그 채널이 설정되어 있지 않아도 제재 자체는 정상 실행되며, 성공 여부를 True/False로 반환합니다.
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str, punisher: str = "시스템 자동 검열") -> bool:
    if not guild or not member:
        return False

    # 로그 채널은 선택 사항 (없어도 제재는 진행)
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
            await member.ban(reason=f"전과 20범 달성 ({reason})")
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
                # 타임아웃은 이미 적용된 상태이므로 로그만 남기고 성공으로 처리
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

# ================= [ 이벤트 처리 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 가동되었습니다.")
    try:
        synced = await bot.tree.sync()
        print(f"🔗 {len(synced)}개의 슬래시 명령어가 성공적으로 동기화되었습니다.")
    except Exception as e:
        print(f"❌ 슬래시 명령어 동기화 실패: {e}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages and channel.permissions_for(guild.me).create_public_threads:
            try:
                msg = await channel.send("🛡️ **낫또봇이 입장했습니다! 서버 보안 연동을 시작합니다.**")
                thread = await msg.create_thread(
                    name="🛡️ 낫또봇 보안 연동 가이드 스레드",
                    auto_archive_duration=1440,
                    reason="초기 입장 가이드 제공"
                )
                
                embed = discord.Embed(
                    title="⚙️ 낫또봇 초기 셋업 매뉴얼 (필독)",
                    description="서버에 안전한 보안 인프라를 구축하기 위해 아래 가이드를 읽고 설정을 마쳐주세요.",
                    color=discord.Color.blue()
                )
                
                guide_value = "• `/인증방설정 채널:[#채널]`\n• `/제재로그설정 채널:[#채널]`\n• `/인증지급 역할:[@역할]` (여러 개 등록 가능)\n• `/인증메세지 메세지:[텍스트]`\n\n자세한 사용법은 채팅창에 **`/가이드`**를 입력하여 확인하세요!"

                embed.add_field(
                    name="📌 초기 필수 명령어 목록",
                    value=guide_value,
                    inline=False
                )
                await thread.send(embed=embed)
                break
            except Exception:
                continue

@bot.event
async def on_member_join(member: discord.Member):
    guild_id = member.guild.id
    if guild_id not in GUILD_SETTINGS:
        return

    settings = GUILD_SETTINGS[guild_id]
    auth_channel_id = settings.get("auth_channel")
    join_log_id = settings.get("join_log_channel")
    auth_form = settings.get("auth_form")

    if auth_channel_id and auth_form:
        auth_channel = member.guild.get_channel(auth_channel_id)
        if auth_channel:
            converted_form = auth_form.replace("\\n", "\n").replace("\n", "\n")
            welcome_text = f"👋 {member.mention}님, 서버에 오신 것을 환영합니다!\n{converted_form}"
            await auth_channel.send(welcome_text)

    if join_log_id:
        join_log_channel = member.guild.get_channel(join_log_id)
        if join_log_channel:
            now = datetime.datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분')
            created_at = member.created_at.strftime('%Y년 %m월 %d일 %H시 %M분')
            
            embed = discord.Embed(title="📥 유저 서버 입장", color=discord.Color.green())
            embed.add_field(name="👤 대상 유저", value=f"{member.mention} ({member.name})", inline=False)
            embed.add_field(name="⏰ 입장 시간", value=now, inline=True)
            embed.add_field(name="📅 계정 생성일", value=created_at, inline=True)
            
            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)
            await join_log_channel.send(embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    guild_id = member.guild.id
    if guild_id not in GUILD_SETTINGS:
        return

    leave_log_id = GUILD_SETTINGS[guild_id].get("leave_log_channel")
    if leave_log_id:
        leave_log_channel = member.guild.get_channel(leave_log_id)
        if leave_log_channel:
            embed = discord.Embed(title="📤 유저 서버 퇴장", color=discord.Color.red())
            embed.add_field(name="👤 퇴장 유저", value=f"{member.name} (ID: {member.id})", inline=False)
            
            avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
            embed.set_thumbnail(url=avatar_url)
            await leave_log_channel.send(embed=embed)

# ================= [ 실시간 문자 검열 및 자동 제재 엔진 ] =================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    guild_id = message.guild.id
    settings = GUILD_SETTINGS.get(guild_id, {})
    
    ban_words = settings.get("ban_words", [])
    censor_words = settings.get("censor_words", [])

    # 1. 자동 제재 단어 체크 (즉시 삭제 후 차단/타임아웃 및 로그 전송)
    for word in ban_words:
        if word in message.content:
            try:
                # 메시지 즉시 삭제
                await message.delete()
            except discord.Forbidden:
                pass
            
            # 클릭해야만 보이는 메시지 형식 (스포일러 텍스트) 빌드
            spoiler_content = f"||{message.content}||"
            reason = f"자동 제재 대상 단어 [{word}] 포함 문장 작성"
            
            # 수동 제재와 동일한 로그 시스템 가동 (스포일러 처리된 원래 메시지 포함)
            await punish_user(message.guild, message.author, f"{reason}\n포착된 메시지: {spoiler_content}", "시스템 자동 검열")
            return

    # 2. 일반 검열 단어 체크 (스포일러 메시지 형식으로 전환)
    for word in censor_words:
        if word in message.content:
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            
            # 클릭해야만 보이고 보낸 사람을 알 수 있게 재송출
            await message.channel.send(f"⚠️ {message.author.mention}님의 메시지에 검열 대상 단어가 포함되어 스포일러 처리되었습니다:\n||{message.content}||")
            return

    await bot.process_commands(message)

# ================= [ 슬래시 명령어 전역 에러 핸들러 ] =================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ 이 명령어를 사용할 권한(관리자 권한 등)이 없습니다.", ephemeral=True)
    elif isinstance(error, app_commands.BotMissingPermissions):
        await interaction.response.send_message("❌ 봇에게 필요한 권한이 부족하여 명령어를 실행할 수 없습니다.", ephemeral=True)
    else:
        print(f"슬래시 명령어 오류 발생: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ 명령어를 처리하는 중 알 수 없는 시스템 오류가 발생했습니다.", ephemeral=True)

# ================= [ ⚙️ 관리자 설정 명령어 파트 ] =================

@bot.tree.command(name="제재로그설정", description="서버의 제재 및 전과 내역이 기록될 채널을 지정합니다. (관리자 전용)")
@app_commands.describe(채널="지정할 텍스트 채널을 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def set_punish_log(interaction: discord.Interaction, 채널: discord.TextChannel):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS: GUILD_SETTINGS[guild_id] = {}
    GUILD_SETTINGS[guild_id]["punish_log_channel"] = 채널.id
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"✅ 제재 로그 채널이 {채널.mention}으로 설정되었습니다.", ephemeral=True)

@bot.tree.command(name="제재로그제거", description="설정된 제재 로그 채널을 시스템에서 삭제합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def remove_punish_log(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "punish_log_channel" in GUILD_SETTINGS[guild_id]:
        del GUILD_SETTINGS[guild_id]["punish_log_channel"]
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 제재 로그 채널 설정이 완전히 제거되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 설정된 제재 로그 채널이 없습니다.", ephemeral=True)

@bot.tree.command(name="인증방설정", description="신입 유저가 입장했을 때 양식을 보낼 인증 채널을 지정합니다. (관리자 전용)")
@app_commands.describe(채널="지정할 인증 텍스트 채널을 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def set_auth_channel(interaction: discord.Interaction, 채널: discord.TextChannel):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS: GUILD_SETTINGS[guild_id] = {}
    GUILD_SETTINGS[guild_id]["auth_channel"] = 채널.id
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"✅ 인증 채널이 {채널.mention}으로 설정되었습니다.", ephemeral=True)

@bot.tree.command(name="인증방제거", description="설정된 인증 채널 설정을 삭제합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def remove_auth_channel(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "auth_channel" in GUILD_SETTINGS[guild_id]:
        del GUILD_SETTINGS[guild_id]["auth_channel"]
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 인증 채널 설정이 완전히 제거되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 설정된 인증 채널이 없습니다.", ephemeral=True)

@bot.tree.command(name="입장로그설정", description="유저가 서버에 들어왔을 때 로그를 띄울 채널을 지정합니다. (관리자 전용)")
@app_commands.describe(채널="지정할 입장 로그 채널을 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def set_join_log(interaction: discord.Interaction, 채널: discord.TextChannel):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS: GUILD_SETTINGS[guild_id] = {}
    GUILD_SETTINGS[guild_id]["join_log_channel"] = 채널.id
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"✅ 입장 로그 채널이 {채널.mention}으로 설정되었습니다.", ephemeral=True)

@bot.tree.command(name="입장로그제거", description="설정된 입장 로그 채널을 삭제합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def remove_join_log(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "join_log_channel" in GUILD_SETTINGS[guild_id]:
        del GUILD_SETTINGS[guild_id]["join_log_channel"]
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 입장 로그 채널 설정이 완전히 제거되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 설정된 입장 로그 채널이 없습니다.", ephemeral=True)

@bot.tree.command(name="퇴장로그설정", description="유저가 서버를 나갔을 때 로그를 띄울 채널을 지정합니다. (관리자 전용)")
@app_commands.describe(채널="지정할 퇴장 로그 채널을 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def set_leave_log(interaction: discord.Interaction, 채널: discord.TextChannel):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS: GUILD_SETTINGS[guild_id] = {}
    GUILD_SETTINGS[guild_id]["leave_log_channel"] = 채널.id
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"✅ 퇴장 로그 채널이 {채널.mention}으로 설정되었습니다.", ephemeral=True)

@bot.tree.command(name="퇴장로그제거", description="설정된 퇴장 로그 채널을 삭제합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def remove_leave_log(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "leave_log_channel" in GUILD_SETTINGS[guild_id]:
        del GUILD_SETTINGS[guild_id]["leave_log_channel"]
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 퇴장 로그 채널 설정이 완전히 제거되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 설정된 퇴장 로그 채널이 없습니다.", ephemeral=True)

@bot.tree.command(name="인증지급", description="인증완료 시 지급할 기본 마스터 역할들을 누적 등록합니다. (관리자 전용)")
@app_commands.describe(역할="지급할 기본 역할을 선택하세요. 여러 번 실행 시 다중 등록됩니다.")
@app_commands.default_permissions(administrator=True)
async def set_auth_role(interaction: discord.Interaction, 역할: discord.Role):
    guild_id = interaction.guild.id
    if 역할 >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ 봇의 서열과 같거나 더 높은 역할은 자동 지급용으로 등록할 수 없습니다.", ephemeral=True)
        return

    if guild_id not in GUILD_SETTINGS: 
        GUILD_SETTINGS[guild_id] = {}
    
    if "auth_role_ids" not in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["auth_role_ids"] = []
        
    if 역할.id not in GUILD_SETTINGS[guild_id]["auth_role_ids"]:
        GUILD_SETTINGS[guild_id]["auth_role_ids"].append(역할.id)
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"✅ 기본 마스터 역할 목록에 **{역할.name}**이(가) 추가되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ **{역할.name}**은(는) 이미 기본 역할 목록에 등록되어 있습니다.", ephemeral=True)

@bot.tree.command(name="인증지급초기화", description="인증지급으로 등록된 모든 기본 역할을 초기화합니다. ✅/❌ 반응으로 한 번 더 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def reset_auth_roles(interaction: discord.Interaction):
    await _confirm_and_reset_words(interaction, "auth_role_ids", "인증지급 역할")

@bot.tree.command(name="인증지급제거", description="인증지급으로 등록된 역할 중 선택한 역할 하나만 제외합니다. (관리자 전용)")
@app_commands.describe(역할="인증지급에서 제외할 역할을 목록에서 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def remove_auth_role(interaction: discord.Interaction, 역할: str):
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
    role_name = discord.utils.escape_mentions(discord.utils.escape_markdown(role.name)) if role else f"삭제된 역할({role_id})"
    await interaction.response.send_message(f"🗑️ **{role_name}** 역할이 인증지급에서 제외되었습니다. (남은 역할: {len(role_ids)}개)", ephemeral=True)

@remove_auth_role.autocomplete("역할")
async def remove_auth_role_autocomplete(interaction: discord.Interaction, current: str):
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

@bot.tree.command(name="인증메세지", description="유저가 입장할 때 자동으로 보낼 인증 안내 메세지를 등록합니다. (관리자 전용)")
@app_commands.describe(메세지="인증방에 출력할 문구를 작성하세요. (줄바꿈은 \n 문자를 사용하세요)")
@app_commands.default_permissions(administrator=True)
async def set_auth_form(interaction: discord.Interaction, 메세지: str):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS: GUILD_SETTINGS[guild_id] = {}
    GUILD_SETTINGS[guild_id]["auth_form"] = 메세지
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message("✅ 인증 안내 메세지가 정상적으로 저장되었습니다.", ephemeral=True)


# ================= [ 제재/검열 단어 설정 · 확인 · 삭제 커맨드 ] =================

@bot.tree.command(name="제재단어설정", description="포함 시 즉시 삭제 및 자동 제재 처리할 단어들을 누적 등록합니다. 쉼표(,)로 다중 등록이 가능합니다. (관리자 전용)")
@app_commands.describe(제재단어="제재할 단어들을 적어주세요. 여러 단어는 쉼표(,)로 구분해 한 번에 많이 넣을 수 있습니다.")
@app_commands.default_permissions(administrator=True)
async def set_ban_words(interaction: discord.Interaction, 제재단어: str):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}
    if "ban_words" not in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["ban_words"] = []

    input_words = [w.strip() for w in 제재단어.split(",") if w.strip()]

    added_words = []
    for word in input_words:
        if word not in GUILD_SETTINGS[guild_id]["ban_words"]:
            GUILD_SETTINGS[guild_id]["ban_words"].append(word)
            added_words.append(word)

    if not added_words:
        await interaction.response.send_message("⚠️ 입력한 단어들은 이미 모두 등록되어 있거나 유효하지 않습니다.", ephemeral=True)
        return

    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"🚨 **자동 제재 단어 추가 완료:** `{', '.join(added_words)}` 가 서버 필터에 바인딩되었습니다.", ephemeral=True)

# ---------- 단어 목록 초기화 (✅/❌ 반응으로 이중 확인) ----------
RESET_CONFIRM_TIMEOUT = 60  # 선택 대기 시간(초)

async def _confirm_and_reset_words(interaction: discord.Interaction, key: str, label: str):
    guild_id = interaction.guild.id
    words = GUILD_SETTINGS.get(guild_id, {}).get(key, [])
    if not words:
        await interaction.response.send_message(f"❌ 등록된 {label}{josa(label, '이', '가')} 없어 초기화할 내용이 없습니다.", ephemeral=True)
        return

    warn_text = (
        f"⚠️ **경고: {label} 전체 초기화**\n"
        f"{interaction.user.mention}님, 등록된 {label} **{len(words)}개**가 모두 삭제되며 되돌릴 수 없습니다.\n\n"
        f"✅ 를 누르면 **초기화 진행** / ❌ 를 누르면 **취소**\n"
        f"(명령어를 입력한 본인만 선택할 수 있으며, {RESET_CONFIRM_TIMEOUT}초 안에 선택하지 않으면 자동 취소됩니다.)"
    )
    await interaction.response.send_message(warn_text)
    warn_msg = await interaction.original_response()

    async def delete_warning():
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    async def notice(text: str):
        # 5초 뒤 자동으로 사라지는 안내 메시지
        try:
            await interaction.channel.send(text, delete_after=5)
        except (discord.HTTPException, AttributeError):
            pass

    # 바로 누를 수 있도록 봇이 미리 반응을 달아 둠
    try:
        await warn_msg.add_reaction("✅")
        await warn_msg.add_reaction("❌")
    except discord.HTTPException:
        await delete_warning()
        await interaction.followup.send("❌ 반응(이모지)을 추가할 권한이 없어 초기화를 진행할 수 없습니다. 봇에 '반응 추가하기', '메시지 기록 보기' 권한을 주세요.", ephemeral=True)
        return

    def check(payload: discord.RawReactionActionEvent):
        return (
            payload.message_id == warn_msg.id
            and payload.user_id == interaction.user.id
            and str(payload.emoji) in ("✅", "❌")
        )

    try:
        payload = await bot.wait_for("raw_reaction_add", check=check, timeout=RESET_CONFIRM_TIMEOUT)
    except asyncio.TimeoutError:
        await delete_warning()
        await notice(f"⏰ {interaction.user.mention} 시간이 초과되어 {label} 초기화가 취소되었습니다.")
        return

    await delete_warning()

    if str(payload.emoji) == "✅":
        if guild_id in GUILD_SETTINGS and GUILD_SETTINGS[guild_id].get(key):
            GUILD_SETTINGS[guild_id][key] = []
            save_settings(GUILD_SETTINGS)
        try:
            await interaction.followup.send(f"🗑️ 등록된 {label} {len(words)}개가 모두 초기화되었습니다.", ephemeral=True)
        except discord.HTTPException:
            pass
    else:
        await notice(f"🚫 {interaction.user.mention} {label} 초기화가 거절(취소)되었습니다. 기존 설정은 그대로 유지됩니다.")

@bot.tree.command(name="제재단어초기화", description="등록된 모든 자동 제재 단어를 초기화합니다. ✅/❌ 반응으로 한 번 더 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def reset_ban_words(interaction: discord.Interaction):
    await _confirm_and_reset_words(interaction, "ban_words", "제재 단어")

@bot.tree.command(name="검열단어초기화", description="등록된 모든 스포일러 검열 단어를 초기화합니다. ✅/❌ 반응으로 한 번 더 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def reset_censor_words(interaction: discord.Interaction):
    await _confirm_and_reset_words(interaction, "censor_words", "검열 단어")

@bot.tree.command(name="검열단어설정", description="포함 시 메시지를 스포일러 기법으로 변경 처리할 단어들을 누적 등록합니다. 쉼표(,)로 다중 등록이 가능합니다. (관리자 전용)")
@app_commands.describe(검열단어="검열할 단어들을 적어주세요. 여러 단어는 쉼표(,)로 구분해 한 번에 많이 넣을 수 있습니다.")
@app_commands.default_permissions(administrator=True)
async def set_censor_words(interaction: discord.Interaction, 검열단어: str):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}
    if "censor_words" not in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["censor_words"] = []

    input_words = [w.strip() for w in 검열단어.split(",") if w.strip()]

    added_words = []
    for word in input_words:
        if word not in GUILD_SETTINGS[guild_id]["censor_words"]:
            GUILD_SETTINGS[guild_id]["censor_words"].append(word)
            added_words.append(word)

    if not added_words:
        await interaction.response.send_message("⚠️ 입력한 검열 단어들은 이미 모두 등록되어 있거나 유효하지 않습니다.", ephemeral=True)
        return

    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"🔒 **스포일러 검열 단어 추가 완료:** `{', '.join(added_words)}` 가 서버 필터에 누적 등록되었습니다.", ephemeral=True)

# ---------- 단어 목록 확인 ----------
async def _show_words(interaction: discord.Interaction, key: str, label: str, emoji: str):
    words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
    if not words:
        await interaction.response.send_message(f"❌ 등록된 {label}가 없습니다.", ephemeral=True)
        return

    word_text = ", ".join(f"`{w}`" for w in words)
    if len(word_text) > 3900:
        word_text = word_text[:3900] + " …(일부 생략)"
    embed = discord.Embed(
        title=f"{emoji} 등록된 {label} ({len(words)}개)",
        description=word_text,
        color=discord.Color.orange()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="제재단어확인", description="지금까지 등록한 자동 제재 단어 목록을 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_ban_words(interaction: discord.Interaction):
    await _show_words(interaction, "ban_words", "제재 단어", "🚨")

@bot.tree.command(name="검열단어확인", description="지금까지 등록한 스포일러 검열 단어 목록을 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_censor_words(interaction: discord.Interaction):
    await _show_words(interaction, "censor_words", "검열 단어", "🔒")


# ---------- 단어 개별 삭제 (자동완성으로 목록에서 선택) ----------
def _word_choices(interaction: discord.Interaction, key: str, current: str):
    if not interaction.guild:
        return []
    words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
    cur = current.lower()
    return [
        app_commands.Choice(name=w[:100], value=w)
        for w in words if cur in w.lower() and len(w) <= 100
    ][:25]

async def _remove_word(interaction: discord.Interaction, key: str, label: str, word: str):
    guild_id = interaction.guild.id
    words = GUILD_SETTINGS.get(guild_id, {}).get(key, [])
    if word not in words:
        await interaction.response.send_message(f"❌ `{word}` 은(는) 등록된 {label}가 아닙니다. 목록에서 선택해 주세요.", ephemeral=True)
        return
    words.remove(word)
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"🗑️ {label} `{word}` 이(가) 삭제되었습니다. (남은 단어: {len(words)}개)", ephemeral=True)

@bot.tree.command(name="제재단어삭제", description="등록된 제재 단어 중 선택한 단어 하나만 삭제합니다. (관리자 전용)")
@app_commands.describe(단어="삭제할 제재 단어를 목록에서 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def remove_ban_word(interaction: discord.Interaction, 단어: str):
    await _remove_word(interaction, "ban_words", "제재 단어", 단어)

@remove_ban_word.autocomplete("단어")
async def remove_ban_word_autocomplete(interaction: discord.Interaction, current: str):
    return _word_choices(interaction, "ban_words", current)

@bot.tree.command(name="검열단어삭제", description="등록된 검열 단어 중 선택한 단어 하나만 삭제합니다. (관리자 전용)")
@app_commands.describe(단어="삭제할 검열 단어를 목록에서 선택하세요.")
@app_commands.default_permissions(administrator=True)
async def remove_censor_word(interaction: discord.Interaction, 단어: str):
    await _remove_word(interaction, "censor_words", "검열 단어", 단어)

@remove_censor_word.autocomplete("단어")
async def remove_censor_word_autocomplete(interaction: discord.Interaction, current: str):
    return _word_choices(interaction, "censor_words", current)


# ================= [ 설정 현황 확인 커맨드 ] =================
async def _show_channel_setting(interaction: discord.Interaction, key: str, label: str):
    channel_id = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key)
    if not channel_id:
        await interaction.response.send_message(f"❌ 설정된 {label}이(가) 없습니다.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(channel_id)
    channel_text = channel.mention if channel else "(삭제되었거나 접근할 수 없는 채널)"
    await interaction.response.send_message(f"📌 **현재 {label}:** {channel_text}\n🆔 채널 ID: `{channel_id}`", ephemeral=True)

@bot.tree.command(name="제재로그확인", description="설정된 제재 로그 채널과 ID를 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_punish_log(interaction: discord.Interaction):
    await _show_channel_setting(interaction, "punish_log_channel", "제재 로그 채널")

@bot.tree.command(name="인증방확인", description="설정된 인증 채널과 ID를 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_auth_channel(interaction: discord.Interaction):
    await _show_channel_setting(interaction, "auth_channel", "인증 채널")

@bot.tree.command(name="입장로그확인", description="설정된 입장 로그 채널과 ID를 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_join_log(interaction: discord.Interaction):
    await _show_channel_setting(interaction, "join_log_channel", "입장 로그 채널")

@bot.tree.command(name="퇴장로그확인", description="설정된 퇴장 로그 채널과 ID를 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_leave_log(interaction: discord.Interaction):
    await _show_channel_setting(interaction, "leave_log_channel", "퇴장 로그 채널")

@bot.tree.command(name="인증메세지확인", description="등록된 인증 안내 메세지를 확인합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def show_auth_form(interaction: discord.Interaction):
    form = GUILD_SETTINGS.get(interaction.guild.id, {}).get("auth_form")
    if not form:
        await interaction.response.send_message("❌ 등록된 인증 메세지가 없습니다.", ephemeral=True)
        return
    preview = form.replace("\\n", "\n")
    if len(preview) > 3900:
        preview = preview[:3900] + "\n…(일부 생략)"
    embed = discord.Embed(title="📝 등록된 인증 메세지", description=preview, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ================= [ 🛠️ 유저 관리 및 가이드 명령어 파트 ] =================

@bot.tree.command(name="인증완료", description="대상 대기자 유저에게 기본 역할과 성별/특수 역할을 목록에서 직접 선택해 원격 지급합니다.")
@app_commands.describe(유저="인증을 승인할 유저를 선택하세요.", 추가역할="성별(남자/여자)이나 추가로 얹어줄 특수 역할을 목록에서 선택하세요. (선택사항)")
@app_commands.default_permissions(manage_roles=True)
async def approve_user(interaction: discord.Interaction, 유저: discord.Member, 추가역할: discord.Role = None):
    if not await require_setting_channel(interaction, "auth_channel", "인증방", "/인증방설정", "인증 승인"):
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
        await interaction.followup.send("❌ 지급할 기본 역할이나 추가역할이 없습니다. 설정을 먼저 진행해 주세요.")
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
            roles_text = ", ".join([f"**{r.name}**" for r in roles_to_add])
            await target_channel.send(f"🎉 축하합니다 {유저.mention}님! 서류 인증이 승인되어 멤버 역할({roles_text}) 자격이 활성화되었습니다.")
        
        await interaction.followup.send(f"✅ {유저.name}님에게 성공적으로 {len(roles_to_add)}개의 역할을 지급하고 승인했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 서열(역할 순위)이 타겟 유저 또는 지급 역할보다 낮아 처리에 실패했습니다.")

@bot.tree.command(name="제재", description="멤버 목록에서 유저를 선택해 수동 제재합니다. (타임아웃 + 전과 부여)")
@app_commands.describe(멤버="제재할 멤버를 목록에서 검색하여 선택하세요.", 사유="제재 사유를 입력하세요.")
@app_commands.default_permissions(moderate_members=True)
async def manual_punish_slash(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 제재"):
    if not await require_setting_channel(interaction, "punish_log_channel", "제재 로그 채널", "/제재로그설정", "제재"):
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

@bot.tree.command(name="추방", description="멤버 목록에서 유저를 선택하고 사유를 적어 서버에서 추방합니다.")
@app_commands.describe(멤버="추방할 멤버를 목록에서 검색하여 선택하세요.", 사유="추방 사유를 입력하세요. (감사 로그와 제재 로그에 기록됩니다)")
@app_commands.default_permissions(kick_members=True)
async def kick_member_slash(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 추방"):
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
        await 멤버.kick(reason=f"{사유} (집행자: {interaction.user.name})"[:512])
    except discord.Forbidden:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n봇에게 '멤버 추방하기' 권한이 없거나 역할 순위가 부족합니다.")
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n`{e}`")
        return

    safe_reason = discord.utils.escape_mentions(사유)
    await interaction.followup.send(f"✅ **추방 완료:** {display}\n📝 **사유:** {safe_reason}")

    # 제재 로그 채널이 설정되어 있으면 기록을 남김 (설정이 없어도 추방은 정상 처리)
    log_channel_id = GUILD_SETTINGS.get(guild.id, {}).get("punish_log_channel")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if log_channel:
        embed = discord.Embed(title="👢 유저 추방 알림", color=discord.Color.red(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 대상자", value=f"{멤버.mention} ({멤버.name})", inline=True)
        embed.add_field(name="🔨 집행자", value=str(interaction.user.name), inline=True)
        embed.add_field(name="💬 추방 사유", value=safe_reason[:1024], inline=False)
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass

@bot.tree.command(name="제재지우기", description="멤버 목록에서 유저를 선택해 타임아웃을 풀고 전과 단계를 1범 감면합니다.")
@app_commands.describe(멤버="제재 복구 대상 멤버를 목록에서 검색하여 선택하세요.")
@app_commands.default_permissions(moderate_members=True)
async def clear_punish_slash(interaction: discord.Interaction, 멤버: discord.Member):
    if not await require_setting_channel(interaction, "punish_log_channel", "제재 로그 채널", "/제재로그설정", "제재지우기"):
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    log_channel_id = GUILD_SETTINGS[guild.id]["punish_log_channel"]
    log_channel = guild.get_channel(log_channel_id)

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

@bot.tree.command(name="가이드", description="보안 시스템 각 커맨드 사용법과 세팅 누락 시 생기는 불이익을 확인합니다.")
async def send_system_guide(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    setup_text = (
        "• **/제재로그설정 `[#채널]`** / **/제재로그제거** / **/제재로그확인**\n"
        "• **/인증방설정 `[#채널]`** / **/인증방제거** / **/인증방확인**\n"
        "• **/입장로그설정** / **/입장로그제거** / **/입장로그확인**\n"
        "• **/퇴장로그설정** / **/퇴장로그제거** / **/퇴장로그확인**\n"
        "  └ 설정은 채널 지정, 제거는 설정 삭제, 확인은 현재 설정된 채널과 ID를 보여줍니다. (미설정 시 '없다'고 안내)"
    )

    auth_text = (
        "• **/인증지급 `[@역할]`** : 승인 완료 시 줄 기본 역할을 등록합니다. (여러 번 입력 시 누적 추가)\n"
        "• **/인증지급초기화** : 등록된 기본 역할을 모두 비웁니다. (경고 메시지의 ✅ 누르면 초기화, ❌ 누르면 취소)\n"
        "• **/인증지급제거** : 등록된 역할 중 선택한 역할 하나만 제외합니다.\n"
        "• **/인증메세지 `[문구]`** : 신입 입장 시 안내할 메세지를 저장합니다. (`\\n`으로 줄바꿈 가능)\n"
        "• **/인증메세지확인** : 저장된 인증 메세지를 확인합니다.\n"
        "• **/인증완료 `[@유저]` `[@추가역할]`** : 기본 역할과 선택한 성별/특수 역할을 함께 지급합니다."
    )

    word_text = (
        "• **/제재단어설정 `[단어1, 단어2]`** : 포함 시 삭제 + 자동 제재(타임아웃·전과)할 단어를 등록합니다.\n"
        "• **/제재단어확인** : 등록된 제재 단어 목록을 확인합니다.\n"
        "• **/제재단어삭제 `[단어]`** : 목록에서 선택한 제재 단어 하나만 삭제합니다.\n"
        "• **/검열단어설정 `[단어1, 단어2]`** : 포함 시 스포일러 처리할 단어를 등록합니다.\n"
        "• **/검열단어확인** : 등록된 검열 단어 목록을 확인합니다.\n"
        "• **/검열단어삭제 `[단어]`** : 목록에서 선택한 검열 단어 하나만 삭제합니다.\n"
        "• **/제재단어초기화 / /검열단어초기화** : 해당 단어 목록 전체를 비웁니다. (경고 메시지의 ✅ 누르면 초기화, ❌ 누르면 취소)"
    )

    penalty_text = (
        "1. **인증 시스템 마비 (Critical)**\n"
        "   - `인증방설정`, `인증지급`, `인증메세지` 세 가지 중 하나라도 데이터가 누락되면 신입 유저가 유입되어도 **인증 메세지가 안 뜨거나 역할을 주지 못해 가입 단계가 완전히 멈추게 됩니다.** (`인증방설정`이 없으면 `/인증완료`도 실행되지 않습니다.)\n"
        "2. **보안 모니터링 블랙아웃 (Blackout)**\n"
        "   - `제재로그설정`이 비어있으면 **`/제재`, `/제재지우기`를 사용할 수 없고**, 자동 제재는 처리되지만 **로그방에 기록(임베드)이 남지 않아 관리망이 마비**됩니다. 입퇴장 로그 셋업이 비어있으면 해당 로그가 기록되지 않습니다."
    )

    embed = discord.Embed(
        title="🛡️ 낫또봇 보안 관리 인프라 연동 매뉴얼",
        description="하드코딩이 완전히 제거된 범용 봇 버전입니다. 각 모듈 명령어 양식과 필수 주의사항을 확인하세요.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="⚙️ 채널 설정 명령어", value=setup_text, inline=False)
    embed.add_field(name="✅ 인증 명령어", value=auth_text, inline=False)
    embed.add_field(name="🚨 제재/검열 단어 명령어", value=word_text, inline=False)
    embed.add_field(name="🚨 채널/역할 설정 누락 시 생기는 관리자 불이익", value=penalty_text, inline=False)
    embed.set_footer(text="안전하고 청정한 서버 환경을 구축합니다 • Powered by NattoBot")
    
    await interaction.followup.send(embed=embed)

# ================= [ 실행 ] =================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
