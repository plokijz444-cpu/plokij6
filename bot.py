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
# ⚠️ 현재 운영 중이신 3개 서버의 실제 ID와 제재방 ID를 정확히 맞춰주세요.
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
    """유저의 '전과 역할'을 완벽하게 분석하여 0부터 1씩 순차적으로 올리는 핵심 함수"""
    if not guild or guild.id not in SERVER_CONFIG:
        return 

    log_channel_id = SERVER_CONFIG[guild.id]
    log_channel = guild.get_channel(log_channel_id)
    if not log_channel:
        try: log_channel = await bot.fetch_channel(log_channel_id)
        except Exception: log_channel = None

    # 유저가 현재 가진 역할을 실시간으로 조사하여 최고 전과 숫자를 파악합니다.
    current_count = 0
    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            try:
                # "전과 3범" -> 3 이라는 숫자를 정수로 추출
                current_count = int(r.name.replace("전과 ", "").replace("범", "").strip())
                break
            except ValueError:
                continue

    # 전과가 아예 없거나 지워져서 0인 상태라면 -> 1범부터 차례대로 상승시킵니다.
    current_count += 1

    try:
        # 20범 도달 시 즉시 영구 차단(BAN)
        if current_count >= 20:
            await member.ban(reason=f"전과 20범 달성 ({reason})")
            if log_channel:
                await log_channel.send(f"🚨 **{member.mention}**님이 전과 20범이 되어 서버에서 **영구 차단(BAN)** 되었습니다.\n사유: {reason}\n집행자: {punisher}")
            return

        role_name = f"전과 {current_count}범"
        
        # 유저가 이전에 가졌던 과거의 모든 전과 역할 자국을 완전 청소합니다.
        for r in member.roles:
            if r.name.startswith("전과 ") and r.name.endswith("범"):
                await member.remove_roles(r)

        # 새로운 단계의 전과 역할이 서버에 없다면 자동으로 생성합니다.
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")
        
        # 새 등급의 전과 역할 부여 및 타임아웃 적용 (최대 28일 제한 우회)
        await member.add_roles(role)
        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        await member.timed_out_until(discord.utils.utcnow() + duration, reason=f"{role_name} 제재 ({reason})")

        # 각 서버에 매칭된 독립 제재방에 정확하게 알림 전송 (집행자 명시)
        if log_channel:
            await log_channel.send(
                f"⚠️ **제재 알림**\n"
                f"👤 **대상자**: {member.mention}\n"
                f"🔨 **집행자**: {punisher}\n"
                f"📜 **조치**: {role_name} 부여 및 {punish_days}일간 타임아웃\n"
                f"💬 **사유**: {reason}"
            )
            
    except discord.Forbidden:
        # 봇 권한(서버 내 역할 순위) 문제 발생 시 안내 로그 출력
        if log_channel:
            await log_channel.send(f"❌ **오류 발생**: 봇의 역할 순위가 낮아 **{member.mention}**님을 제재하지 못했습니다. 서버 설정 ➡️ 역할에서 봇의 순위를 맨 위로 드래그해 올려주세요.")
    except Exception as e:
        print(f"Error handling punishment: {e}")

# ================= [ 이벤트 및 명령어 처리 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 에러 없이 가동되었습니다.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # ⭐ [구조 전면 수정] 명령어(!제재 등)를 입력하면 검열을 무조건 건너뛰고 명령어를 정상 즉시 작동시킵니다.
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
    # 관리자가 입력한 명령어 문장을 즉시 완벽 삭제
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    # 명령어를 실행한 관리자(ctx.author.mention)를 집행자로 기록
    await punish_user(ctx.guild, member, "관리자 수동 제재", ctx.author.mention)

# !제재지우기 @사용자 명령어
@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    # 관리자가 입력한 명령어 문장을 즉시 완벽 삭제
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
        # 타임아웃 초기화 및 강제 해제
        await member.timed_out_until(None, reason="관리자가 제재 해제")

        # 해당 유저가 가졌던 모든 전과 역할 강제 철거
        for r in member.roles:
            if r.name.startswith("전과 ") and r.name.endswith("box") or (r.name.startswith("전과 ") and r.name.endswith("범")):
                await member.remove_roles(r)

        # 알림방에 해제한 관리자 명시하여 알림 전송
        if log_channel:
            await log_channel.send(f"🔓 **제재 해제**: {ctx.author.mention} 관리자가 {member.mention}님의 타임아웃 및 전과 단계를 초기화했습니다.")
            
    except discord.Forbidden:
        if log_channel:
            await log_channel.send(f"❌ **해제 실패**: 봇의 역할 순위가 부족하여 제재를 풀지 못했습니다.")

# ================= [ 봇 실행 ] =================
keep_alive() 
bot.run(os.getenv("DISCORD_TOKEN"))
