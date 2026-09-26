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
# ⚠️ 중요: 본인의 실제 디스코드 서버 ID와 채널 ID로 변경하세요!
SERVER_CONFIG = {
    1548279076890869770: 1553218649471451168,  # 서버 1 ID : 제재방 1 ID
    1529347274403086468: 1546457831631224843,  # 서버 2 ID : 제재방 2 ID
    1543921157101854820: 1553219199084798054   # 서버 3 ID : 제재방 3 ID
}

# 자동 검열 금지어 목록 (원하는 단어로 바꾸세요)
BANNED_WORDS = ["장애","애미","느금","너엄","너애미","너애비","느금마","느금빠","느개비","느그애비","애비","창녀","창년","보지","봊이","자지","섹스","섹x","정액"]

# 전과 기록 저장소 { 유저ID : 전과_횟수 }
criminal_records = {}

intents = discord.Intents.default()
intents.message_content = True  # 메시지 내용 읽기 권한
intents.members = True          # 서버 멤버 관리 권한

bot = commands.Bot(command_prefix="!", intents=intents)

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str):
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
        await member.ban(reason=f"전과 20범 달성 ({reason})")
        if log_channel:
            await log_channel.send(f"🚨 **{member.mention}**님이 전과 20범이 되어 서버에서 **영구 차단(BAN)** 되었습니다. (사유: {reason})")
        return

    # 전과 역할 이름 생성
    role_name = f"전과 {current_count}범"
    
    # 기존 '전과 X범' 역할 제거
    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            await member.remove_roles(r)

    # 역할 찾기 또는 서버에 새로 생성하기
    role = discord.utils.get(guild.roles, name=role_name)
    if not role:
        role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")
    
    await member.add_roles(role)

    # 타임아웃 시간 계산 (전과 수 만큼 '일' 단위 추가)
    duration = datetime.timedelta(days=current_count)
    await member.timed_out_until(discord.utils.utcnow() + duration, reason=f"{role_name} 제재 ({reason})")

    if log_channel:
        await log_channel.send(
            f"⚠️ **제재 알림**\n"
            f"👤 **대상자**: {member.mention}\n"
            f"📜 **조치**: {role_name} 부여 및 {current_count}일간 타임아웃\n"
            f"💬 **사유**: {reason}"
        )

# ================= [ 이벤트 및 명령어 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 준비되었습니다!")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.content == "안녕":
        await message.channel.send(f"안녕하세요, {message.author.mention}님! 반가워요.")
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
    await ctx.send(f"👌 {member.mention} 유저를 제재했습니다.", delete_after=5)

@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    guild_id = ctx.guild.id
    if guild_id not in SERVER_CONFIG:
        return

    log_channel_id = SERVER_CONFIG[guild_id]
    log_channel = ctx.guild.get_channel(log_channel_id)

    await member.timed_out_until(None, reason="관리자가 제재 해제")

    for r in member.roles:
        if r.name.startswith("전과 ") and r.name.endswith("범"):
            await member.remove_roles(r)

    if member.id in criminal_records:
        del criminal_records[member.id]

    await ctx.send(f"✅ {member.mention}님의 제재가 해제되었습니다.", delete_after=5)
    if log_channel:
        await log_channel.send(f"🔓 **제재 해제**: 관리자가 {member.mention}님의 타임아웃 및 전과 역할을 삭제하고 제재를 풀었습니다.")

# ================= [ 봇 실행 ] =================
keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))
