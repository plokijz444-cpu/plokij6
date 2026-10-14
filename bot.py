import discord
from discord.ext import commands
import datetime
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

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str, punisher: str = "시스템 자동 검열"):
    if not guild or guild.id not in GUILD_SETTINGS:
        return 

    log_channel_id = GUILD_SETTINGS[guild.id].get("punish_log_channel")
    if not log_channel_id:
        return

    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        return

    if member.top_role >= guild.me.top_role:
        embed = discord.Embed(title="❌ 제재 권한 실패 경고", color=discord.Color.dark_red())
        embed.description = f"봇의 역할 순위가 대상 유저({member.mention})보다 낮거나 같아 제재할 수 없습니다."
        await log_channel.send(embed=embed)
        return

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
            embed.add_field(name="💬 최종 사유", value=reason, inline=False)
            embed.set_footer(text="억울한 사항이 있다면 관리자에게 증거와 함께 자초지종을 DM으로 보내주세요")
            await log_channel.send(embed=embed)
            return

        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        await member.edit(timed_out_until=target_time, reason=f"전과 {current_count}범 제재 ({reason})")

        role_name = f"전과 {current_count}범"
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            try:
                role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")
            except discord.HTTPException as e:
                await log_channel.send(f"❌ **역할 생성 실패 (서버 제한 초과 가능성):** `{e.text}`")
                return

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        embed = discord.Embed(title="⚠️ 유저 제재 알림", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
        embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
        embed.add_field(name="📜 조치 내용", value=f"**{role_name}** 부여 및 **{punish_days}일간** 타임아웃", inline=False)
        embed.add_field(name="💬 제재 사유", value=reason, inline=False)
        embed.set_footer(text="억울한 사항이 있다면 관리자에게 증거와 함께 자초지종을 DM으로 보내주세요")
        await log_channel.send(embed=embed)
            
    except discord.Forbidden:
        embed = discord.Embed(title="❌ 제재 권한 실패 경고", color=discord.Color.dark_red())
        embed.description = f"봇의 역할 순위가 {member.mention}님보다 낮아 제재 처리에 실패했습니다. 서버 설정에서 봇 역할을 위로 올려주세요."
        await log_channel.send(embed=embed)
    except Exception as e:
        await log_channel.send(f"❌ **제재 실행 중 시스템 에러 발생:** `{e}`")

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
                
                guide_value = "• `/인증방설정 채널:[#채널]`\n• `/제재로그설정 채널:[#채널]`\n• `/인증지급 역할:[@역할]` (여러 개 등록 가능)\n• `/인증양식 양식:[텍스트]`\n\n자세한 사용법은 채팅창에 **`/가이드`**를 입력하여 확인하세요!"

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

@bot.tree.command(name="인증지급리셋", description="등록되어 있던 모든 기본 마스터 역할 목록을 초기화합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def reset_auth_roles(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "auth_role_ids" in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["auth_role_ids"] = []
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 등록된 모든 기본 마스터 역할 리스트가 청소되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 등록된 역할 목록이 존재하지 않습니다.", ephemeral=True)

@bot.tree.command(name="인증양식", description="유저가 입장할 때 자동으로 보낼 가이드 안내 양식 문구를 등록합니다. (관리자 전용)")
@app_commands.describe(양식="인증방에 출력할 문구를 작성하세요. (줄바꿈은 \n 문자를 사용하세요)")
@app_commands.default_permissions(administrator=True)
async def set_auth_form(interaction: discord.Interaction, 양식: str):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS: GUILD_SETTINGS[guild_id] = {}
    GUILD_SETTINGS[guild_id]["auth_form"] = 양식
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message("✅ 인증 가이드 및 양식 문장 정상 보관되었습니다.", ephemeral=True)


# ================= [ 🆕 자동 제재 및 검열 언어 다중 설정 커맨드 ] =================

@bot.tree.command(name="자동제재언어설정", description="포함 시 즉시 삭제 및 자동 제재 처리할 단어들을 누적 등록합니다. 쉼표(,)로 다중 등록이 가능합니다. (관리자 전용)")
@app_commands.describe(제재언어="제재할 단어들을 적어주세요. 여러 단어는 쉼표(,)로 구분해 한 번에 많이 넣을 수 있습니다.")
@app_commands.default_permissions(administrator=True)
async def set_ban_words(interaction: discord.Interaction, 제재언어: str):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}
    if "ban_words" not in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["ban_words"] = []

    # 쉼표로 분리하고 공백 제거 및 빈 문자열 필터링
    input_words = [w.strip() for w in 제재언어.split(",") if w.strip()]
    
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

@bot.tree.command(name="자동제재언어초기화", description="등록되어 작동 중인 모든 자동 제재 대상 언어 데이터베이스를 초기화합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def reset_ban_words(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "ban_words" in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["ban_words"] = []
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 모든 자동 제재 언어 목록이 청소되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 등록된 제재 언어 리스트가 없습니다.", ephemeral=True)


@bot.tree.command(name="검열언어설정", description="포함 시 메시지를 스포일러 기법으로 변경 처리할 단어들을 누적 등록합니다. 쉼표(,)로 다중 등록이 가능합니다. (관리자 전용)")
@app_commands.describe(검열언어="검열할 단어들을 적어주세요. 여러 단어는 쉼표(,)로 구분해 한 번에 많이 넣을 수 있습니다.")
@app_commands.default_permissions(administrator=True)
async def set_censor_words(interaction: discord.Interaction, 검열언어: str):
    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}
    if "censor_words" not in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["censor_words"] = []

    # 쉼표로 분리하고 공백 제거 및 빈 문자열 필터링 (다중 입력 완벽 지원)
    input_words = [w.strip() for w in 검열언어.split(",") if w.strip()]
    
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

@bot.tree.command(name="검열언어초기화", description="등록되어 작동 중인 모든 스포일러 검열 대상 언어 데이터베이스를 초기화합니다. (관리자 전용)")
@app_commands.default_permissions(administrator=True)
async def reset_censor_words(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and "censor_words" in GUILD_SETTINGS[guild_id]:
        GUILD_SETTINGS[guild_id]["censor_words"] = []
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message("🗑️ 모든 스포일러 검열 언어 목록이 초기화되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ 등록된 검열 언어 리스트가 없습니다.", ephemeral=True)


# ================= [ 🛠️ 유저 관리 및 가이드 명령어 파트 ] =================

@bot.tree.command(name="인증완료", description="대상 대기자 유저에게 기본 역할과 성별/특수 역할을 목록에서 직접 선택해 원격 지급합니다.")
@app_commands.describe(유저="인증을 승인할 유저를 선택하세요.", 추가역할="성별(남자/여자)이나 추가로 얹어줄 특수 역할을 목록에서 선택하세요. (선택사항)")
@app_commands.default_permissions(manage_roles=True)
async def approve_user(interaction: discord.Interaction, 유저: discord.Member, 추가역할: discord.Role = None):
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

@bot.tree.command(name="제재", description="멤버 목록에서 유저를 정확히 지정해 수동 제재 및 전과 단계를 누적합니다.")
@app_commands.describe(멤버="제재 대상 멤버를 목록에서 검색하여 선택하세요.", 사유="제재 사유를 입력하세요.")
@app_commands.default_permissions(moderate_members=True)
async def manual_punish_slash(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 제재"):
    await interaction.response.defer(ephemeral=True)
    
    if 멤버.top_role >= interaction.guild.me.top_role:
        await interaction.followup.send("❌ 봇의 역할 순위가 대상 유저보다 낮거나 같아 제재 처리에 실패했습니다.")
        return
        
    await punish_user(interaction.guild, 멤버, 사유, str(interaction.user.name))
    await interaction.followup.send(f"⚠️ {멤버.name} 유저에게 즉각적인 제재 및 전과 패널티를 부여했습니다.")

@bot.tree.command(name="제재지우기", description="멤버 목록에서 유저를 선택해 타임아웃을 풀고 전과 단계를 1범 감면합니다.")
@app_commands.describe(멤버="제재 복구 대상 멤버를 목록에서 검색하여 선택하세요.")
@app_commands.default_permissions(moderate_members=True)
async def clear_punish_slash(interaction: discord.Interaction, 멤버: discord.Member):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    if guild.id not in GUILD_SETTINGS or "punish_log_channel" not in GUILD_SETTINGS[guild.id]:
        await interaction.followup.send("❌ 시스템 설정에 제재 로그방 정보가 없습니다. `/제재로그설정`을 먼저 실행하세요.")
        return

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
        action_description = "대상자의 타임아웃 해제 및 전과 누적 단계를 전면 백지화(초기화)했습니다."

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

        await interaction.followup.send(f"🔓 {멤버.name} 유저의 제재 스택 감면 집행을 완료했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 권한 등급(서열)이 멤버 권한보다 낮아 조치를 거부당했습니다.")
    except Exception as e:
        await interaction.followup.send(f"❌ 예외 에러 발생: {e}")

@bot.tree.command(name="가이드", description="보안 시스템 각 커맨드 사용법과 세팅 누락 시 생기는 불이익을 확인합니다.")
async def send_system_guide(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    guide_text = (
        "• **/제재로그설정 `[#채널]`** : 유저 제재 및 전과 내역이 기록될 채널을 지정합니다.\n"
        "• **/제재로그제거** : 시스템에 연결된 제재 채널 데이터를 완전 파기합니다.\n"
        "• **/인증방설정 `[#채널]`** : 가입자가 최초 진입 시 인증 서식을 송출해 줄 타겟 채널을 셋업합니다.\n"
        "• **/인증방제거** : 지정된 인증 채널 설정을 초기화합니다.\n"
        "• **/입장로그설정 / /퇴장로그설정** : 입퇴장 유저 추적 데이터를 전송받을 로그 채널을 연동합니다.\n"
        "• **/입장로그제거 / /퇴장로그제거** : 바인딩된 입퇴장 방 정보를 테이블에서 삭제합니다.\n"
        "• **/인증지급 `[@역할]`** : 승인 완료 시 줄 기본 역할들을 등록합니다. (여러 번 입력 시 누적 추가)\n"
        "• **/인증지급리셋** : 등록되어 있던 모든 기본 마스터 역할 목록을 비웁니다.\n"
        "• **/인증양식 `[문구]`** : 신입 멤버 입장 즉시 안내할 질문지 양식을 저장합니다. (`\\n`으로 줄바꿈 가능)\n"
        "• **/인증완료 `[@유저]` `[@추가역할]`** : 대상 유저에게 기본 역할들과 선택한 성별/특수 역할을 함께 지급합니다.\n"
        "• **/자동제재언어설정 `[단어1, 단어2]`** : 제재할 언어들을 쉼표로 구분하여 여러 개 등록합니다.\n"
        "• **/검열언어설정 `[단어1, 단어2]`** : 스포일러 처리할 언어들을 쉼표로 구분하여 여러 개 등록합니다."
    )

    penalty_text = (
        "1. **인증 시스템 마비 (Critical)**\n"
        "   - `인증방설정`, `인증지급`, `인증양식` 세 가지 중 하나라도 데이터가 누락되면 신입 유저가 유입되어도 **인증 양식이 안 뜨거나 역할을 주지 못해 가입 단계가 완전히 멈추게 됩니다.**\n"
        "2. **보안 모니터링 블랙아웃 (Blackout)**\n"
        "   - `제재로그설정` 또는 입퇴장 로그 셋업이 비어있을 경우, 봇이 내부적으로 제재 및 타임아웃 조치는 취하지만 **로그방에 아무런 기록(임베드)을 남기지 않아 관리망이 마비**됩니다."
    )

    embed = discord.Embed(
        title="🛡️ 낫또봇 보안 관리 인프라 연동 매뉴얼",
        description="하드코딩이 완전히 제거된 범용 봇 버전입니다. 각 모듈 명령어 양식과 필수 주의사항을 확인하세요.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="⚙️ 시스템 통합 명령어 구성표", value=guide_text, inline=False)
    embed.add_field(name="🚨 채널/역할 설정 누락 시 생기는 관리자 불이익", value=penalty_text, inline=False)
    embed.set_footer(text="안전하고 청정한 서버 환경을 구축합니다 • Powered by NattoBot")
    
    await interaction.followup.send(embed=embed)

# ================= [ 실행 ] =================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
