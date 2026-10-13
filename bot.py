import discord
from discord.ext import commands
import datetime
from flask import Flask
from threading import Thread
import os
import typing
import json
import asyncio
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

# ================= [ 데이터베이스 (JSON) 관리 로직 ] =================
DATA_FILE = "guild_settings.json"

def load_settings():
    """서버별 설정을 파일에서 불러옵니다."""
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    except Exception:
        return {}

def save_settings(settings):
    """서버별 설정을 파일에 저장합니다."""
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in settings.items()}, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"❌ 데이터 저장 중 오류 발생: {e}")

# 봇 가동 시 데이터 메모리 로드
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
        try: log_channel = await bot.fetch_channel(log_channel_id)
        except Exception: log_channel = None

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
        # 1. 전과 20범 이상 영구 차단 검사 우선 진행
        if current_count >= 20:
            await member.ban(reason=f"전과 20범 달성 ({reason})")
            if log_channel:
                embed = discord.Embed(title="🚨 유저 영구 차단 (BAN)", color=discord.Color.red(), timestamp=discord.utils.utcnow())
                embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
                embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
                embed.add_field(name="💬 최종 사유", value=reason, inline=False)
                embed.set_footer(text="억울한 사항이 있다면 관리자에게 증거와 함께 자초지종을 DM으로 보내주세요")
                await log_channel.send(embed=embed)
            return

        # 2. 타임아웃 처리
        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        await member.edit(timed_out_until=target_time, reason=f"전과 {current_count}범 제재 ({reason})")

        # 3. 전과 역할 업데이트 로직 진행
        role_name = f"전과 {current_count}범"
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        # 4. 제재방 로그 전송
        if log_channel:
            embed = discord.Embed(title="⚠️ 유저 제재 알림", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
            embed.add_field(name="📜 조치 내용", value=f"**{role_name}** 부여 및 **{punish_days}일간** 타임아웃", inline=False)
            embed.add_field(name="💬 제재 사유", value=reason, inline=False)
            embed.set_footer(text="억울한 사항이 있다면 관리자에게 증거와 함께 자초지종을 DM으로 보내주세요")
            await log_channel.send(embed=embed)
            
    except discord.Forbidden:
        if log_channel:
            embed = discord.Embed(title="❌ 제재 권한 실패 경고", color=discord.Color.dark_red())
            embed.description = f"봇의 역할 순위가 {member.mention}님보다 낮아 제재 처리에 실패했습니다. 서버 설정에서 봇 역할을 위로 올려주세요."
            await log_channel.send(embed=embed)
    except Exception as e:
        if log_channel:
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
    """봇이 서버에 새로 입장하면 가이드를 위해 스레드를 만들거나 메시지를 보내고, /가이드 사용을 안내합니다."""
    # 서버의 첫 번째 텍스트 채널을 찾아 안내 메시지 전송 및 스레드 생성 시도
    target_channel = None
    for channel in guild.text_channels:
        if channel.permissions_for(guild.me).send_messages:
            target_channel = channel
            break
            
    if target_channel:
        embed = discord.Embed(
            title="📥 보안 인증 및 제재 관리 봇 입장 환영",
            description="서버에 봇이 초대되었습니다! 안전한 서버 환경 구축을 위해 초기 가이드 스레드를 생성합니다.",
            color=discord.Color.blue()
        )
        embed.add_field(name="📖 가이드 확인", value="현재 채널 아래에 생성된 스레드 가이드 또는 `/가이드` 명령어를 통해 필수 설정법을 확인해 주세요.", inline=False)
        main_msg = await target_channel.send(embed=embed)
        
        # 가이드를 위한 퍼블릭 스레드 생성
        if target_channel.permissions_for(guild.me).create_public_threads:
            try:
                thread = await main_msg.create_thread(name="🛡️ 낫또봇 보안 연동 가이드 스레드", auto_archive_duration=60)
                
                guide_embed = discord.Embed(
                    title="📖 낫또봇 전체 기능 및 명령어 사용 가이드 매뉴얼",
                    description="서버 관리자님 반갑습니다! 본 봇을 유기적으로 연동하려면 각 채널과 역할을 먼저 설정해야 합니다.",
                    color=discord.Color.purple()
                )
                guide_embed.add_field(
                    name="⚙️ 1. 채널 설정 및 제거 커맨드",
                    value=(
                        "• **/제재로그설정 `제재방id`** : 유저 제재 및 전과 내역이 기록될 채널을 지정합니다.
"
                        "• **/제재로그제거** : 제재 로그방 설정을 파기합니다.
"
                        "• **/인증방설정 `인증방id`** : 유저가 들어왔을 때 양식이 발송될 채널을 지정합니다.
"
                        "• **/인증방제거** : 인증방 설정을 파기합니다.
"
                        "• **/입장로그설정 `입장로그id`** : 유저 입장 정보가 기록될 채널을 지정합니다.
"
                        "• **/입장로그제거** : 입장 로그방 설정을 파기합니다.
"
                        "• **/퇴장로그설정 `퇴장로그id`** : 유저 퇴장 정보가 기록될 채널을 지정합니다.
"
                        "• **/퇴장로그제거** : 퇴장 로그방 설정을 파기합니다."
                    ),
                    inline=False
                )
                guide_embed.add_field(
                    name="🎫 2. 인증 관리 및 양식 설정 커맨드",
                    value=(
                        "• **/인증지급 `줄역할`** : 앞으로 `/인증완료`를 집행할 때 유저에게 자동 지급할 역할을 설정합니다.
"
                        "• **/인증양식 `낫또봇이보낼말`** : 새 유저가 서버에 입장했을 때 설정된 인증방에 보낼 환영/인증 양식 문구를 저장합니다.
"
                        "• **/인증완료 `대상`** : `target`에 입력된 유저에게 `/인증지급`으로 설정한 모든 역할을 원클릭으로 지급하고 인증을 성공 처리합니다."
                    ),
                    inline=False
                )
                guide_embed.add_field(
                    name="⚠️ 3. 미설정 시 발생하는 시스템 불이익 및 경고",
                    value=(
                        "• **채널 누락 시**: 해당 기능의 자동 알림, 입장 안내 양식 전송, 입퇴장 모니터링 로그 전송 과정이 통째로 생략됩니다.
"
                        "• **역할 누락 시**: `/인증지급`을 통해 역할을 하나도 매핑해두지 않은 경우, 관리자가 `/인증완료`를 실행해도 유저에게 아무런 권한 역할이 부여되지 않아 유저가 대기방에 격리될 수 있습니다.
"
                        "• **봇 서열 경고**: 디스코드 시스템 규칙에 따라, 봇의 역할 등급이 부여하려는 역할보다 밑에 있으면 권한 실패 오류가 발생하므로 반드시 봇 역할을 최상단으로 올려주세요."
                    ),
                    inline=False
                )
                await thread.send(embed=guide_embed)
            except Exception as e:
                print(f"스레드 생성 오류: {e}")

@bot.event
async def on_member_join(member: discord.Member):
    guild_id = member.guild.id
    if guild_id not in GUILD_SETTINGS:
        return

    settings = GUILD_SETTINGS[guild_id]
    auth_channel_id = settings.get("auth_channel")
    join_log_id = settings.get("join_log_channel")
    auth_template = settings.get("auth_template")

    # 1. 설정된 인증방이 있고, 인증 양식 문구가 설정되어 있으면 해당 문구 발송
    if auth_channel_id and auth_template:
        auth_channel = member.guild.get_channel(auth_channel_id) or await bot.fetch_channel(auth_channel_id)
        if auth_channel:
            # 멘션을 문구 앞에 자연스럽게 치환하거나 추가
            formatted_msg = f"{member.mention}\n{auth_template}" if "{mention}" not in auth_template else auth_template.replace("{mention}", member.mention)
            await auth_channel.send(formatted_msg.replace("\n", "
"))

    # 2. 입장 로그방 메시지 발송
    if join_log_id:
        join_log_channel = member.guild.get_channel(join_log_id) or await bot.fetch_channel(join_log_id)
        if join_log_channel:
            now = datetime.datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분')
            created_at = member.created_at.strftime('%Y년 %m월 %d일 %H시 %M분')
            
            embed = discord.Embed(title="📥 유저 서버 입장", color=discord.Color.green())
            embed.add_field(name="👤 대상 유저", value=f"{member.mention} ({member.name})", inline=False)
            embed.add_field(name="⏰ 입장 시간", value=now, inline=True)
            embed.add_field(name="📅 계정 생성일", value=created_at, inline=True)
            
            if member.avatar: embed.set_image(url=member.avatar.url)
            else: embed.set_image(url=member.default_avatar.url)
                
            await join_log_channel.send(content=f"안녕하세요 {member.mention}님! 저희 서버에 오신것을 환영해요", embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    guild_id = member.guild.id
    if guild_id not in GUILD_SETTINGS:
        return

    leave_log_id = GUILD_SETTINGS[guild_id].get("leave_log_channel")
    if leave_log_id:
        leave_log_channel = member.guild.get_channel(leave_log_id) or await bot.fetch_channel(leave_log_id)
        if leave_log_channel:
            embed = discord.Embed(title="📤 유저 서버 퇴장", color=discord.Color.red())
            embed.description = "다음에 만날날이 오길빌게요..."
            embed.add_field(name="👤 퇴장 유저", value=f"{member.name} (ID: {member.id})", inline=False)
            
            if member.avatar: embed.set_image(url=member.avatar.url)
            else: embed.set_image(url=member.default_avatar.url)
                
            await leave_log_channel.send(embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if message.content == "안녕":
        await message.channel.send(f"안녕하세요, {message.author.mention}님! 반가워요.")
        return

# ================= [ 🛠️ 동적 채널 설정 및 제거 슬래시 커맨드 ] =================

async def validate_and_save_channel(interaction: discord.Interaction, channel_id_str: str, key_name: str, display_name: str):
    """채널 ID 형식을 검증하고 JSON 데이터베이스에 저장하는 공통 함수"""
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        await interaction.followup.send("❌ 채널 ID는 숫자 형식으로만 입력하셔야 합니다. (개발자 모드에서 복사한 값)")
        return

    channel_check = interaction.guild.get_channel(channel_id)
    if not channel_check:
        await interaction.followup.send("❌ 이 서버에 존재하지 않는 채널 ID이거나 봇이 접근할 수 없는 채널입니다.")
        return

    guild_id = interaction.guild.id
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}

    GUILD_SETTINGS[guild_id][key_name] = channel_id
    save_settings(GUILD_SETTINGS)
    await interaction.followup.send(f"✅ 성공적으로 **{display_name}** 채널이 {channel_check.mention} (ID: {channel_id})로 설정되었습니다!")

async def remove_channel_setting(interaction: discord.Interaction, key_name: str, display_name: str):
    """저장된 채널 설정을 안전하게 파기하는 공통 함수"""
    guild_id = interaction.guild.id
    if guild_id in GUILD_SETTINGS and key_name in GUILD_SETTINGS[guild_id]:
        del GUILD_SETTINGS[guild_id][key_name]
        save_settings(GUILD_SETTINGS)
        await interaction.followup.send(f"🗑️ 이 서버의 **{display_name}** 채널 설정이 완전히 삭제 및 파기되었습니다.")
    else:
        await interaction.followup.send(f"ℹ️ 설정된 {display_name} 채널 정보가 데이터베이스에 존재하지 않습니다.")

# 1. 제재로그설정 및 제거
@bot.tree.command(name="제재로그설정", description="유저 제재 및 전과 내역 임베드가 들어갈 제재방 채널 ID를 설정합니다. (관리자 전용)")
@app_commands.describe(제재방id="제재 기록을 송신할 텍스트 채널의 고유 ID 숫자를 적어주세요.")
@commands.has_permissions(administrator=True)
async def set_punish_log(interaction: discord.Interaction, 제재방id: str):
    await interaction.response.defer(ephemeral=True)
    await validate_and_save_channel(interaction, 제재방id, "punish_log_channel", "제재 로그방")

@bot.tree.command(name="제재로그제거", description="기존에 등록된 제재 로그방 채널 연동을 완전히 제거합니다. (관리자 전용)")
@commands.has_permissions(administrator=True)
async def remove_punish_log(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await remove_channel_setting(interaction, "punish_log_channel", "제재 로그방")

# 2. 인증방설정 및 제거
@bot.tree.command(name="인증방설정", description="신규 입출입자들의 인증 양식 문구를 띄울 인증방 채널 ID를 설정합니다. (관리자 전용)")
@app_commands.describe(인증방id="인증 패널 및 문구를 송신할 텍스트 채널의 고유 ID 숫자를 적어주세요.")
@commands.has_permissions(administrator=True)
async def set_auth_channel(interaction: discord.Interaction, 인증방id: str):
    await interaction.response.defer(ephemeral=True)
    await validate_and_save_channel(interaction, 인증방id, "auth_channel", "인증방")

@bot.tree.command(name="인증방제거", description="기존에 등록된 인증방 채널 연동을 완전히 제거합니다. (관리자 전용)")
@commands.has_permissions(administrator=True)
async def remove_auth_channel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await remove_channel_setting(interaction, "auth_channel", "인증방")

# 3. 입장로그설정 및 제거
@bot.tree.command(name="입장로그설정", description="새로운 유저의 입장 시간과 프로필 로그가 들어갈 채널 ID를 설정합니다. (관리자 전용)")
@app_commands.describe(입장로그id="입장 기록 임베드를 송신할 텍스트 채널의 고유 ID 숫자를 적어주세요.")
@commands.has_permissions(administrator=True)
async def set_join_log(interaction: discord.Interaction, 입장로그id: str):
    await interaction.response.defer(ephemeral=True)
    await validate_and_save_channel(interaction, 입장로그id, "join_log_channel", "입장 로그방")

@bot.tree.command(name="입장로그제거", description="기존에 등록된 입장 로그방 채널 연동을 완전히 제거합니다. (관리자 전용)")
@commands.has_permissions(administrator=True)
async def remove_join_log(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await remove_channel_setting(interaction, "join_log_channel", "입장 로그방")

# 4. 퇴장로그설정 및 제거
@bot.tree.command(name="퇴장로그설정", description="유저가 서버를 나갔을 때 송별 로그가 들어갈 채널 ID를 설정합니다. (관리자 전용)")
@app_commands.describe(퇴장로그id="퇴장 기록 임베드를 송신할 텍스트 채널의 고유 ID 숫자를 적어주세요.")
@commands.has_permissions(administrator=True)
async def set_leave_log(interaction: discord.Interaction, 퇴장로그id: str):
    await interaction.response.defer(ephemeral=True)
    await validate_and_save_channel(interaction, 퇴장로그id, "leave_log_channel", "퇴장 로그방")

@bot.tree.command(name="퇴장로그제거", description="기존에 등록된 퇴장 로그방 채널 연동을 완전히 제거합니다. (관리자 전용)")
@commands.has_permissions(administrator=True)
async def remove_leave_log(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await remove_channel_setting(interaction, "leave_log_channel", "퇴장 로그방")

# ================= [ 🎫 인증 관리 및 지급 커스텀 슬래시 커맨드 ] =================

@bot.tree.command(name="인증지급", description="앞으로 /인증완료를 실행할 때 유저에게 자동 지급할 역할을 지정합니다. (관리자 전용)")
@app_commands.describe(줄역할="인증 성공 시 일괄 부여할 역할을 멘션으로 직접 선택해 주세요.")
@commands.has_permissions(administrator=True)
async def set_auth_roles(interaction: discord.Interaction, 줄역할: discord.Role):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}
        
    # 유연한 연동을 위해 역할의 ID 고유번호를 리스트에 삽입하여 보관 (다중 보관 확장 고려)
    GUILD_SETTINGS[guild_id]["auth_role_id"] = 줄역할.id
    save_settings(GUILD_SETTINGS)
    
    await interaction.followup.send(f"✅ 앞으로 `/인증완료` 집행 시 **{줄역할.name}** 역할이 자동 지급되도록 세팅되었습니다!")

@bot.tree.command(name="인증양식", description="유저가 서버에 신규 입장할 때 인증방에 띄울 안내 가이드 템플릿 문구를 설정합니다. (관리자 전용)")
@app_commands.describe(낫또봇이보낼말="유저가 들어왔을 때 인증방에 출력할 안내 서식 문장을 적어주세요. 줄바꿈은 \n 으로 가능합니다.")
@commands.has_permissions(administrator=True)
async def set_auth_template(interaction: discord.Interaction, 낫또봇이보낼말: str):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild.id
    
    if guild_id not in GUILD_SETTINGS:
        GUILD_SETTINGS[guild_id] = {}
        
    GUILD_SETTINGS[guild_id]["auth_template"] = 낫또봇이보낼말
    save_settings(GUILD_SETTINGS)
    
    await interaction.followup.send("✅ 서버 가입 시 인증방에 전송될 맞춤형 인증양식 양식 문장이 정상 저장되었습니다!")

@bot.tree.command(name="인증완료", description="대상 유저를 승인하고 /인증지급으로 지정했던 설정을 기반으로 역할을 자동 지급합니다.")
@app_commands.describe(대상="인증 완료 처리를 집행할 대상 멤버를 선택하세요.")
@commands.has_permissions(manage_roles=True)
async def approve_auth_universal(interaction: discord.Interaction, 대상: discord.Member):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    guild_id = guild.id

    # 데이터베이스에 저장된 역할과 인증방 채널 세팅값 확인
    settings = GUILD_SETTINGS.get(guild_id, {})
    auth_role_id = settings.get("auth_role_id")
    auth_channel_id = settings.get("auth_channel")

    target_channel = guild.get_channel(auth_channel_id) if auth_channel_id else interaction.channel

    if not auth_role_id:
        await interaction.followup.send("❌ 먼저 관리자가 `/인증지급` 명령어를 실행하여 지급할 역할을 시스템에 연결해 주셔야 합니다!")
        return

    target_role = guild.get_role(auth_role_id)
    if not target_role:
        await interaction.followup.send("❌ 등록된 역할 ID가 서버 역할 목록에 존재하지 않습니다. 다시 설정해 주세요.")
        return

    try:
        # 지정된 공통 역할 인벤토리에 바로 쏙 부여
        await 대상.add_roles(target_role, reason="낫또봇 범용 인증 시스템 통과완료")
        
        # 지정된 인증방 또는 현재 입력 채널로 축하 메시지 방출
        if target_channel:
            await target_channel.send(f"🎉 축하합니다 {대상.mention}님! 인증이 무사히 완료되어 **{target_role.name}** 권한이 지급되었습니다.")
        
        await interaction.followup.send(f"✅ {대상.name}님의 일괄 역할 지급 및 인증 가동 처리를 정상 완수했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 권한 등급(역할 서열)이 대상 유저나 지급하려는 역할보다 낮아 부여에 실패했습니다. 서열을 조절해 주세요.")

# ================= [ ⚖️ 기존 수동 제재 및 감면 슬래시 커맨드 유지 ] =================

@bot.tree.command(name="제재", description="유저에게 수동 제재 누적 및 전과 타임아웃 단계를 집행합니다.")
@app_commands.describe(member="제재를 집행할 대상 서버 멤버를 지정하세요.", reason="제재 사유 문구를 입력하세요.")
@commands.has_permissions(moderate_members=True)
async def manual_punish_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "관리자 수동 제재"):
    await interaction.response.defer(ephemeral=True)
    await punish_user(interaction.guild, member, reason, str(interaction.user.name))
    await interaction.followup.send(f"⚠️ {member.name} 유저에게 수동 제재 전과 스택 처리를 완료했습니다.")

@bot.tree.command(name="제재지우기", description="유저의 타임아웃을 강제 해제하고 전과 단계를 1범 감면 차감합니다.")
@app_commands.describe(member="제재 스택을 해제하거나 감면할 대상 멤버를 선택하세요.")
@commands.has_permissions(moderate_members=True)
async def clear_punish_slash(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    
    if guild.id not in GUILD_SETTINGS or "punish_log_channel" not in GUILD_SETTINGS[guild.id]:
        await interaction.followup.send("❌ 이 서버는 시스템에 제재 로그방 채널이 바인딩되어 있지 않습니다. `/제재로그설정`을 선행해 주세요.")
        return

    log_channel_id = GUILD_SETTINGS[guild.id]["punish_log_channel"]
    log_channel = guild.get_channel(log_channel_id)

    try:
        await member.edit(timed_out_until=None, reason="관리자가 제재 해제")

        current_count = 0
        roles_to_remove = []
        for r in member.roles:
            if r.name.startswith("전과 ") and r.name.endswith("범"):
                try:
                    current_count = int(r.name.replace("전과 ", "").replace("범", "").strip())
                    roles_to_remove.append(r)
                except ValueError:
                    continue

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)

        next_count = max(0, current_count - 1)
        action_description = "대상자의 타임아웃 및 모든 전과 단계를 완전히 초기화했습니다."

        if next_count > 0:
            new_role_name = f"전과 {next_count}범"
            role = discord.utils.get(guild.roles, name=new_role_name) or await guild.create_role(name=new_role_name)
            await member.add_roles(role)
            action_description = f"타임아웃 해제 후 전과 단계를 1범 하향 조정했습니다. (**전과 {current_count}범** ➡️ **{new_role_name}**)"

        if log_channel:
            embed = discord.Embed(title="🔓 유저 제재 감면 및 해제", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 실행 관리자", value=str(interaction.user.name), inline=True)
            embed.description = action_description
            await log_channel.send(embed=embed)

        await interaction.followup.send(f"🔓 {member.name} 유저의 제재 감면 스택 변경 조치를 마무리했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 권한 부족으로 인해 해당 유저의 타임아웃 해제 조치를 완성하지 못했습니다.")

# ================= [ 📖 종합 가이드 매뉴얼 슬래시 커맨드 ] =================

@bot.tree.command(name="가이드", description="낫또봇의 전체 명령어 가이드라인과 셋업 매뉴얼을 호출합니다.")
async def universal_guide_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False) # 채널 내부 인원 전체 공유
    
    embed = discord.Embed(
        title="🛡️ 낫또봇 보안 관리 및 연동 명령어 가이드라인",
        description="본 가이드는 하드코딩된 서버 제한 사양을 철폐하고, 완전한 멀티 서버 환경을 지원하는 범용 명령어 가이드북입니다.",
        color=discord.Color.purple(),
        timestamp=discord.utils.utcnow()
    )
    
    embed.add_field(
        name="⚙️ 1. 실시간 채널 연결 및 파기 명령어",
        value=(
            "• `/제재로그설정 [제재방ID]` / `/제재로그제거` : 제재 및 감면 임베드가 저장될 고유 연동 채널의 등록 및 파기 기능
"
            "• `/인증방설정 [인증방ID]` / `/인증방제거` : 서버 신규 입장 시 설정된 인증 양식 문구를 송신할 방의 등록 및 파기 기능
"
            "• `/입장로그설정 [입장로그ID]` / `/입장로그제거` : 유저 입장 데이터 임베드를 축적할 로그방의 등록 및 파기 기능
"
            "• `/퇴장로그설정 [퇴장로그ID]` / `/퇴장로그제거` : 유저 퇴장 송별 임베드를 축적할 로그방의 등록 및 파기 기능"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🎫 2. 인증 규칙 및 조건 커스텀 명령어",
        value=(
            "• `/인증지급 [@줄역할]` : 앞으로 `/인증완료`를 타이핑할 때 유저에게 자동 부여할 권한 역할을 시스템 데이터에 맵핑
"
            "• `/인증양식 [문구 내용]` : 새 유저가 들어올 시 인증방 채널에 자동으로 송신해 줄 문장 서식을 영구 기억 시킴
"
            "• `/인증완료 [@유저]` : 사전에 매핑해 둔 역할을 기반으로 일괄 권한을 부여하고 축하 메시지를 동적 타겟팅 송출"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚠️ 4. 개별 요소를 바인딩하지 않았을 때 생기는 시스템 불이익",
        value=(
            "1. **채널 연동을 완전히 제거하거나 비워둔 경우**
"
            "   └ 해당 파트의 자동화 모니터링 기능(입퇴장 추적 알림, 전과 로그 아카이브 축적)이 일절 무시되며 누락 처리됩니다.
"
            "2. **인증지급 역할을 지정하지 않고 완료 명령어를 친 경우**
"
            "   └ 인증 통과 메시지는 생성되나, 유저 인벤토리에 아무런 권한이 들어가지 않아 일반 대화방이 활성화되지 않는 격리 현상이 생깁니다.
"
            "3. **인증양식을 적지 않은 경우**
"
            "   └ 새로운 인원이 들어왔을 때 양식 안내문 생성이 스킵되므로 직접 양식을 설명해야 하는 불편이 초래됩니다."
        ),
        inline=False
    )
    
    embed.set_footer(text="안전하고 스마트한 커뮤니티 관리 통합 솔루션 • Powered by Natto Bot")
    await interaction.followup.send(embed=embed)

# ================= [ 실행 ] =================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
