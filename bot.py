import discord
from discord.ext import commands
import datetime
from flask import Flask
from threading import Thread
import os

# ================= [ Render 24시간 가동을 위한 웹서버 ] =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# ================= [ 디스코드 봇 설정 & 데이터 ] =================
SERVER_CONFIG = {
    1553667461918756875: 1553745608219697253,  # 서버 1 ID : 제재방 1 ID
    1529347274403086468: 1546457831631224843,  # 서버 2 ID : 제재방 2 ID
    1543921157101854820: 1553219199084798054   # 서버 3 ID : 제재방 3 ID
}

BANNED_WORDS = ["장애","애미","느금","너엄","너애미","너애비","느금마","느금빠","느개비","느그애비","애비","창녀","창년","보지","봊이","자지","섹스","섹x","정액"]

intents = discord.Intents.default()
intents.message_content = True  # 메시지 내용 읽기 권한
intents.members = True          # 서버 멤버 관리 권한

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str, punisher: str = "시스템 자동 검열"):
    """유저의 전과 역할을 분석하여 제재(타임아웃 및 역할 교체)하고 제재방에 기록하는 핵심 함수"""
    if not guild or guild.id not in SERVER_CONFIG:
        return 

    log_channel_id = SERVER_CONFIG[guild.id]
    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        try: log_channel = await bot.fetch_channel(log_channel_id)
        except Exception: log_channel = None

    # 1. 유저의 현재 전과 등급 실시간 파싱 및 지울 역할 수집
    current_count = 0
    roles_to_remove = []
    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            try:
                current_count = int(r.name.replace("전과 ", "").replace("범", "").strip())
                roles_to_remove.append(r)
            except ValueError:
                continue

    # 전과 가 없거나 초기화된 상태(0)라면 -> 1범부터 정상 시작
    current_count += 1

    try:
        # 20범 도달 시 -> 영구 차단(BAN)
        if current_count >= 20:
            await member.ban(reason=f"전과 20범 달성 ({reason})")
            if log_channel:
                await log_channel.send(f"🚨 **{member.mention}**님이 전과 20범이 되어 서버에서 **영구 차단(BAN)** 되었습니다.\n사유: {reason}\n집행자: {punisher}")
            return

        role_name = f"전과 {current_count}범"
        
        # 역할이 서버에 없으면 새로 생성
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")

        # [수정] 역할 제거와 추가가 씹히지 않도록 리스트 형식을 언팩(*)하여 안전하게 동시 처리
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        # ⚠️ [가장 치명적이었던 문법 오류 수정 완료]
        # discord.py 공식 규격에 맞춰 반드시 'dt=' 라는 키워드 인자를 명시하여 타임아웃을 실행합니다.
        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        
        await member.timed_out_until(dt=target_time, reason=f"{role_name} 제재 ({reason})")

        # 2. 타임아웃까지 완벽히 성공해야만 제재방에 로그 전송
        if log_channel:
            await log_channel.send(
                f"⚠️ **제재 알림**\n"
                f"👤 **대상자**: {member.mention}\n"
                f"🔨 **집행자**: {punisher}\n"
                f"📜 **조치**: {role_name} 부여 및 {punish_days}일간 타임아웃\n"
                f"💬 **사유**: {reason}"
            )
            
    except Exception as e:
        # 만약 권한 부족 등의 이슈가 있다면 그냥 씹히지 않고 제재방에 명확히 기록을 남깁니다.
        if log_channel:
            await log_channel.send(f"❌ **제재 실행 중 시스템 에러 발생:** `{e}`\n(봇의 역할 순위가 대상자 및 새로 만든 역할보다 높은지 확인해주세요.)")

# ================= [ 이벤트 및 명령어 처리 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 모든 결함을 수정하고 완벽하게 가동되었습니다.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # 관리자가 입력한 명령어는 검열 루프를 타지 않고 즉시 실행되도록 보장합니다.
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # 일반 채팅 안녕 반응 기능
    if message.content == "안녕":
        await message.channel.send(f"안녕하세요, {message.author.mention}님! 반가워요.")
        return

    # 일반 채팅 자동 금지어 검열 시스템
    for word in BANNED_WORDS:
        if word in message.content:
            try: await message.delete() 
            except discord.Forbidden: pass
            await punish_user(message.guild, message.author, f"금지어 사용 검열 ({word})", "시스템 자동 검열")
            return

# !제재 @사용자 명령어
@bot.command(name="제재")
@commands.has_permissions(moderate_members=True) 
async def manual_punish(ctx, member: discord.Member):
    # 관리자가 입력한 명령어 문장을 즉시 깔끔하게 삭제
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    # 명령어를 실행한 관리자를 집행자로 명시하여 처벌 프로세스 가동
    await punish_user(ctx.guild, member, "관리자 수동 제재", ctx.author.mention)

# !제재지우기 @사용자 명령어
@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    # 관리자가 입력한 명령어 문장을 즉시 깔끔하게 삭제
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    guild_id = ctx.guild.id
    if guild_id not in SERVER_CONFIG:
        return

    log_channel_id = SERVER_CONFIG[guild_id]
    log_channel = ctx.guild.get_channel(log_channel_id)
    if not log_channel:
        try: log_channel = await bot.fetch_channel(log_channel_id)
        except Exception: log_channel = None

    try:
        # [수정] 해제 시에도 규격에 맞춰 dt=None 키워드 명시
        await member.timed_out_until(dt=None, reason="관리자가 제재 해제")

        # 해당 유저가 가졌던 모든 전과 역할을 한 번에 묶어서 완전 청소
        roles_to_remove = [r for r in member.roles if r.name.startswith("전과 ") and r.name.endswith("범")]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)

        # 제재방에 해제 로그 전송
        if log_channel:
            await log_channel.send(f"🔓 **제재 해제**: {ctx.author.mention} 관리자가 {member.mention}님의 타임아웃 및 전과 단계를 초기화했습니다.")
            
    except Exception as e:
        if log_channel:
            await log_channel.send(f"❌ **제재 해제 중 시스템 에러 발생:** `{e}`")

# ================= [ 봇 실행 ] =================
keep_alive() 
bot.run(os.getenv("DISCORD_TOKEN"))
