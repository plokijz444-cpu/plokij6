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

    # 전과 데이터가 0이거나 초기화되었다면 -> 1범부터 순차 시작
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

        # 역할 제거와 추가를 안전하게 교체
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        # ⚠️ [타임아웃 적용법 교정] .edit(timed_out_until=...) 형식을 사용합니다.
        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        
        await member.edit(timed_out_until=target_time, reason=f"{role_name} 제재 ({reason})")

        # 제재방 로그 전송
        if log_channel:
            await log_channel.send(
                f"⚠️ **제재 알림**\n"
                f"👤 **대상자**: {member.mention}\n"
                f"🔨 **집행자**: {punisher}\n"
                f"📜 **조치**: {role_name} 부여 및 {punish_days}일간 타임아웃\n"
                f"💬 **사유**: {reason}"
            )
            
    except discord.Forbidden:
        if log_channel:
            await log_channel.send(f"❌ **제재 실패:** 봇의 역할(Role) 순위가 {member.mention}님이나 '{role_name}' 역할보다 낮습니다. 서버 설정에서 봇의 역할 순위를 맨 위로 올려주세요.")
    except Exception as e:
        if log_channel:
            await log_channel.send(f"❌ **제재 실행 중 시스템 에러 발생:** `{e}`")

# ================= [ 이벤트 및 명령어 처리 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 에러 없이 가동되었습니다.")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # 관리자 명령어가 들어오면 자동 검열을 우회하여 정상 작동 보장
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
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    await punish_user(ctx.guild, member, "관리자 수동 제재", ctx.author.mention)

# !제재지우기 @사용자 명령어
@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
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
        # ⚠️ [수정 완료] 기존의 불가능했던 함수 호출 방식 대신 member.edit를 이용해 타임아웃을 안전하게 해제(None)합니다.
        await member.edit(timed_out_until=None, reason="관리자가 제재 해제")

        # 전과 역할 일괄 제거
        roles_to_remove = [r for r in member.roles if r.name.startswith("전과 ") and r.name.endswith("범")]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)

        if log_channel:
            await log_channel.send(f"🔓 **제재 해제**: {ctx.author.mention} 관리자가 {member.mention}님의 타임아웃 및 전과 단계를 초기화했습니다.")
            
    except discord.Forbidden:
        if log_channel:
            await log_channel.send(f"❌ **해제 실패:** 봇의 역할(Role) 순위가 {member.mention}님보다 낮아 제재를 해제할 수 없습니다.")
    except Exception as e:
        if log_channel:
            await log_channel.send(f"❌ **제재 해제 중 시스템 에러 발생:** `{e}`")

# ================= [ 봇 실행 ] =================
keep_alive() 
bot.run(os.getenv("DISCORD_TOKEN"))
