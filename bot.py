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
    1553762662868058164: 1554055050819670036,  # 서버 1 ID : 제재방 1 ID
    1529347274403086468: 1546457831631224843,  # 서버 2 ID : 제재방 2 ID
    1543921157101854820: 1553219199084798054   # 서버 3 ID : 제재방 3 ID
}

BANNED_WORDS = ["장애","애미","느금","너엄","너애미","너애비","느금마","느금빠","느개비","느그애비","애비","창녀","창년","보지","봊이","자지","섹스","섹x","정액"]

intents = discord.Intents.default()
intents.message_content = True  # 메시지 내용 읽기 권한
intents.members = True          # 서버 멤버 관리 권한

bot = commands.Bot(command_prefix="!", intents=intents)
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
    1553762662868058164: 1554055050819670036,  # 서버 1 ID : 제재방 1 ID
    1529347274403086468: 1546457831631224843,  # 서버 2 ID : 제재방 2 ID
    1543921157101854820: 1553219199084798054   # 서버 3 ID : 제재방 3 ID
}

# ⭐ [추가] 서버 1 전용 인증방 ID 설정
SERVER1_ID = 1553762662868058164
SERVER1_AUTH_CHANNEL_ID = 1554061009302585384

BANNED_WORDS = ["장애","애미","느금","너엄","너애미","너애비","느금마","느금빠","느개비","느그애비","애비","창녀","창년","보지","봊이","자지","섹스","섹x","정액"]

intents = discord.Intents.default()
intents.message_content = True  # 메시지 내용 읽기 권한
intents.members = True          # 서버 멤버 관리 권한 (입장 감지에 필수)

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

    # 전과 1범씩 정상 누적 (0이면 1부터 시작)
    current_count += 1

    try:
        # 20범 도달 시 -> 영구 차단(BAN) 임베드 박스 전송
        if current_count >= 20:
            await member.ban(reason=f"전과 20범 달성 ({reason})")
            if log_channel:
                embed = discord.Embed(title="🚨 유저 영구 차단 (BAN)", color=discord.Color.red(), timestamp=discord.utils.utcnow())
                embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
                embed.add_field(name="🔨 집행자", value=punisher, inline=True)
                embed.add_field(name="💬 최종 사유", value=reason, inline=False)
                embed.set_footer(text="억울한 사항이 있다면 관리자에게 증거와 함께 자초지종을 DM으로 보내주세요")
                await log_channel.send(embed=embed)
            return

        role_name = f"전과 {current_count}범"
        
        # 역할 자동 생성 및 기존 전과 역할 교체
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        # 타임아웃 적용 (최대 28일 제한 보호 적용)
        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        
        await member.edit(timed_out_until=target_time, reason=f"{role_name} 제재 ({reason})")

        # 2. 네모 박스(Embed) 제재 로그 전송
        if log_channel:
            embed = discord.Embed(title="⚠️ 유저 제재 알림", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 집행자", value=punisher, inline=True)
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

# ================= [ 이벤트 및 명령어 처리 ] =================
@bot.event
async def on_ready():
    print(f"🤖 {bot.user.name} 봇이 모든 시스템 수정을 마치고 가동되었습니다.")

# ⭐ [핵심 추가] 서버 1 입장을 감지하여 인증 채널에 맞춤 멘션 메시지를 전송하는 이벤트
@bot.event
async def on_member_join(member: discord.Member):
    # 신규 입장한 서버의 ID가 서버 1 ID인지 확인
    if member.guild.id == SERVER1_ID:
        auth_channel = member.guild.get_channel(SERVER1_AUTH_CHANNEL_ID)
        if not auth_channel:
            try: auth_channel = await bot.fetch_channel(SERVER1_AUTH_CHANNEL_ID)
            except Exception: auth_channel = None

        if auth_channel:
            # 해당 서버의 관리자 권한을 가진 그룹을 태그하거나 단어로 언급할 수 있도록 구성
            # 요청하신 멘션 양식을 완벽히 준수하여 일반 텍스트 전송
            welcome_msg = (
                f"안녕하세요 {member.mention}님! 인증방에 닉/성별/참가경로/@관리자 멘션을 하시면 서버에 참가하실수 있어요. "
                f"단, 인증양식이 다르다면 관리자가 서버에 참가를 시키지 않을수 있으니 조심하세요!"
            )
            await auth_channel.send(welcome_msg)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # 관리자 명령어 우회 처리
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
            original_sentence = message.content
            
            try: await message.delete() 
            except discord.Forbidden: pass
            
            detailed_reason = f"금지어 `[{word}]` 사용 검열\n**[적발된 문장 원본]**\n|| {original_sentence} ||"
            
            await punish_user(message.guild, message.author, detailed_reason, "시스템 자동 검열")
            return

# !제재 @사용자 (사유) 명령어
@bot.command(name="제재")
@commands.has_permissions(moderate_members=True) 
async def manual_punish(ctx, member: discord.Member, *, reason: str = "관리자 수동 제재"):
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    # 명령어를 실행하는 유저의 최상위 역할 순위가 봇의 최상위 역할 순위보다 낮거나 같으면 실행 차단
    if ctx.author.top_role.position <= ctx.guild.me.top_role.position:
        return

    await punish_user(ctx.guild, member, reason, ctx.author.mention)

# !제재지우기 @사용자 명령어
@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    # 명령어를 실행하는 유저의 최상위 역할 순위가 봇의 최상위 역할 순위보다 낮거나 같으면 실행 차단
    if ctx.author.top_role.position <= ctx.guild.me.top_role.position:
        return

    guild_id = ctx.guild.id
    if guild_id not in SERVER_CONFIG:
        return

    log_channel_id = SERVER_CONFIG[guild_id]
    log_channel = ctx.guild.get_channel(log_channel_id)
    if not log_channel:
        try: log_channel = await bot.fetch_channel(log_channel_id)
        except Exception: log_channel = None

    try:
        # 타임아웃 강제 해제
        await member.edit(timed_out_until=None, reason="관리자가 제재 감면 및 해제")

        # 유저의 현재 전과 단계를 파악하고 기존 전과 역할을 수집합니다.
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

        # 전과 한 단계 차감
        next_count = max(0, current_count - 1)

        action_description = ""
        if next_count > 0:
            new_role_name = f"전과 {next_count}범"
            role = discord.utils.get(ctx.guild.roles, name=new_role_name)
            if not role:
                role = await ctx.guild.create_role(name=new_role_name, reason="전과 시스템 자동 생성")
            await member.add_roles(role)
            action_description = f"대상자의 타임아웃을 해제하고 전과 단계를 한 단계 하향 조정했습니다. (**전과 {current_count}범** ➡️ **{new_role_name}**)"
        else:
            action_description = f"대상자의 타임아웃을 해제하고 누적되어 있던 전과 단계를 모두 초기화(0범)했습니다."

        if log_channel:
            embed = discord.Embed(title="🔓 유저 제재 감면 및 해제", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 실행 관리자", value=ctx.author.mention, inline=True)
            embed.description = action_description
            await log_channel.send(embed=embed)
            
    except discord.Forbidden:
        if log_channel:
            await log_channel.send(f"❌ **해제 실패:** 봇의 역할 순위가 낮아 {member.mention}님의 제재를 풀지 못했습니다.")
    except Exception as e:
        if log_channel:
            await log_channel.send(f"❌ **제재 해제 중 시스템 에러 발생:** `{e}`")

# ================= [ 봇 실행 ] =================
keep_alive() 
bot.run(os.getenv("DISCORD_TOKEN"))
