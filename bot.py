import discord
from discord.ext import commands
import datetime
from flask import Flask
from threading import Thread
import os
import asyncio

# ================= [ Render 24시간 가동을 위한 웹서버 ] =================
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    # 렌더의 포트 감지 시스템을 통과하기 위해 포트 강제 고정
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    """메인 프로세스와 충돌 나지 않도록 데몬 스레드로 안전하게 분리 구동합니다."""
    t = Thread(target=run, daemon=True)
    t.start()

# ================= [ 디스코드 봇 설정 & 데이터 ] =================
# ⚠️ 중요: 본인의 실제 디스코드 서버 ID와 채널 ID로 변경하세요!
SERVER_CONFIG = {
    1548279076890869770: 1553218649471451168,  # 서버 1 ID : 제재방 1 ID
    1529347274403086468: 1546457831631224843,  # 서버 2 ID : 제재방 2 ID
    1543921157101854820: 1553219199084798054   # 서버 3 ID : 제재방 3 ID
}

# 자동 검열 금지어 목록
BANNED_WORDS = ["장애","애미","느금","너엄","너애미","너애비","느금마","느금빠","느개비","느그애비","애비","창녀","창년","보지","봊이","자지","섹스","섹x","정액"]

# 전과 기록 저장소 { 유저ID : 전과_횟수 }
criminal_records = {}

# 개발자 포털의 3가지 스위치와 일치하도록 인텐트 설정
intents = discord.Intents.default()
intents.message_content = True  # 메시지 내용 읽기 권한
intents.members = True          # 서버 멤버 관리 권한
intents.presences = True        # 상태 정보 권한

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str):
    if not guild or not member:
        return
        
    guild_id = guild.id
    if guild_id not in SERVER_CONFIG:
        return

    log_channel_id = SERVER_CONFIG[guild_id]
    log_channel = guild.get_channel(log_channel_id)

    user_id = member.id
    current_count = criminal_records.get(user_id, 0) + 1
    criminal_records[user_id] = current_count

    # 20범 도달 시 -> 밴(추방)
    if current_count >= 20:
        try:
            await member.ban(reason=f"전과 20범 달성 ({reason})")
            if log_channel:
                await log_channel.send(f"🚨 **{member.mention}**님이 전과 20범이 되어 서버에서 **영구 차단(BAN)** 되었습니다. (사유: {reason})")
        except discord.Forbidden:
            if log_channel:
                await log_channel.send(f"❌ 봇의 권한/서열이 부족하여 {member.mention}님을 차단하지 못했습니다. 디스코드 설정에서 봇 역할을 위로 올려주세요.")
        except Exception as e:
            print(f"Ban Error: {e}")
        return

    # 전과 역할 이름 생성
    role_name = f"전과 {current_count}범"
    
    # 기존 '전과 X범' 역할 제거
    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            try:
                await member.remove_roles(r)
            except:
                pass

    # 역할 찾기 또는 서버에 새로 생성하기
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        try:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")
        except Exception as e:
            print(f"Role Create Error: {e}")
    
    if role:
        try:
            await member.add_roles(role)
        except Exception as e:
            print(f"Role Add Error: {e}")

    # 타임아웃 시간 계산 (전과 수 만큼 '일' 단위 추가)
    duration = datetime.timedelta(days=current_count)
    try:
        await member.timed_out_until(discord.utils.utcnow() + duration, reason=f"{role_name} 제재 ({reason})")
    except discord.Forbidden:
        if log_channel:
            await log_channel.send(f"❌ 봇의 권한/서열이 부족하여 {member.mention}님을 타임아웃 처리하지 못했습니다.")
        return
    except Exception as e:
        print(f"Timeout Error: {e}")
        return

    if log_channel:
        try:
            await log_channel.send(
                f"⚠️ **제재 알림**\n"
                f"👤 **대상자**: {member.mention}\n"
                f"📜 **조치**: {role_name} 부여 및 {current_count}일간 타임아웃\n"
                f"💬 **사유**: {reason}"
            )
        except:
            pass

# ================= [ 이벤트 및 명령어 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 준비되었습니다!")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.content == "안녕":
        try:
            await message.channel.send(f"안녕하세요, {message.author.mention}님! 반가워요.")
        except:
            pass
        return

    for word in BANNED_WORDS:
        if word in message.content:
            try:
                await message.delete()
            except:
                pass
            await punish_user(message.guild, message.author, f"금지어 사용 검열 ({word})")
            return

    await bot.process_commands(message)

@bot.command(name="제재")
@commands.has_permissions(moderate_members=True)
async def manual_punish(ctx, member: discord.Member):
    await punish_user(ctx.guild, member, "관리자 수동 제재")
    try:
        await ctx.send(f"👌 {member.mention} 유저를 제재했습니다.", delete_after=5)
    except:
        pass

@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    guild_id = ctx.guild.id
    if guild_id not in SERVER_CONFIG:
        return

    log_channel_id = SERVER_CONFIG[guild_id]
    log_channel = ctx.guild.get_channel(log_channel_id)

    try:
        await member.timed_out_until(None, reason="관리자가 제재 해제")
    except:
        pass

    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            try:
                await member.remove_roles(r)
            except:
                pass

    if member.id in criminal_records:
        del criminal_records[member.id]

    try:
        await ctx.send(f"✅ {member.mention}님의 제재가 해제되었습니다.", delete_after=5)
    except:
        pass
        
    if log_channel:
        try:
            await log_channel.send(f"🔓 **제재 해제**: 관리자가 {member.mention}님의 타임아웃 및 전과 역할을 삭제하고 제재를 풀었습니다.")
        except:
            pass

# ================= [ 봇 실행 ] =================
# 웹서버 개설 후 메인 루프 가동
keep_alive()
token = os.getenv("DISCORD_TOKEN")
if token:
    bot.run(token)
else:
    print("Error: DISCORD_TOKEN environment variable is not set.")
