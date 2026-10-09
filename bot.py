import discord
from discord.ext import commands
import datetime
from flask import Flask
from threading import Thread
import os
import typing

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

# ================= [ 디스코드 봇 설정 & 데이터 ] =================
SERVER_CONFIG = {
    1553762662868058164: 1554055050819670036,  # 서버 1 ID : 제재방 1 ID
    1529347274403086468: 1546457831631224843,  # 서버 2 ID : 제재방 2 ID
    1555119957623185419: 1555617599528898690   # 서버 4 ID : 제재방 4 ID
}

SERVER1_ID = 1553762662868058164
SERVER1_AUTH_CHANNEL_ID = 1554061009302585384
SERVER1_JOIN_LOG_CHANNEL_ID = 1554077665739407360   
SERVER1_LEAVE_LOG_CHANNEL_ID = 1554078156695142542  

SERVER2_ID = 1529347274403086468
SERVER2_AUTH_CHANNEL_ID = 1558091070133370900
SERVER2_JOIN_LOG_CHANNEL_ID = 1558096103147311224
SERVER2_LEAVE_LOG_CHANNEL_ID = 1558096131534364692

intents = discord.Intents.default()
intents.message_content = True  
intents.members = True          

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

@bot.event
async def on_member_join(member: discord.Member):
    # Server 1
    if member.guild.id == SERVER1_ID:
        auth_channel = member.guild.get_channel(SERVER1_AUTH_CHANNEL_ID)
        if not auth_channel:
            try: auth_channel = await bot.fetch_channel(SERVER1_AUTH_CHANNEL_ID)
            except Exception: auth_channel = None

        if auth_channel:
            welcome_msg = (
                f"안녕하세요 {member.mention}님! 인증방에 닉/성별/참가경로/@관리자 멘션을 하시면 서버에 참가하실수 있어요. "
                f"단, 인증양식이 다르다면 관리자가 서버에 참가를 시키지 않을수 있으니 조심하세요!"
            )
            await auth_channel.send(welcome_msg)

        join_log_channel = member.guild.get_channel(SERVER1_JOIN_LOG_CHANNEL_ID)
        if not join_log_channel:
            try: join_log_channel = await bot.fetch_channel(SERVER1_JOIN_LOG_CHANNEL_ID)
            except Exception: join_log_channel = None

        if join_log_channel:
            now = datetime.datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분')
            created_at = member.created_at.strftime('%Y년 %m월 %d일 %H시 %M분')
            
            embed = discord.Embed(title="📥 유저 서버 입장", color=discord.Color.green())
            embed.add_field(name="👤 대상 유저", value=f"{member.mention} ({member.name})", inline=False)
            embed.add_field(name="⏰ 입장 시간", value=now, inline=True)
            embed.add_field(name="📅 계정 생성일", value=created_at, inline=True)
            
            if member.avatar: embed.set_image(url=member.avatar.url)
            else: embed.set_image(url=member.default_avatar.url)
                
            await join_log_channel.send(embed=embed)

    # Server 2
    elif member.guild.id == SERVER2_ID:
        auth_channel = member.guild.get_channel(SERVER2_AUTH_CHANNEL_ID)
        if not auth_channel:
            try: auth_channel = await bot.fetch_channel(SERVER2_AUTH_CHANNEL_ID)
            except Exception: auth_channel = None

        if auth_channel:
            embed = discord.Embed(
                title="👋 환영합니다!",
                description=f"안녕하세요 {member.mention}님! 닉/성별/관리자멘션 양식으로 인증을 하여 서버에 참가하세요!",
                color=discord.Color.blue()
            )
            if member.avatar: embed.set_image(url=member.avatar.url)
            else: embed.set_image(url=member.default_avatar.url)
            await auth_channel.send(embed=embed)

        join_log_channel = member.guild.get_channel(SERVER2_JOIN_LOG_CHANNEL_ID)
        if not join_log_channel:
            try: join_log_channel = await bot.fetch_channel(SERVER2_JOIN_LOG_CHANNEL_ID)
            except Exception: join_log_channel = None

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
    # Server 1
    if member.guild.id == SERVER1_ID:
        leave_log_channel = member.guild.get_channel(SERVER1_LEAVE_LOG_CHANNEL_ID)
        if not leave_log_channel:
            try: leave_log_channel = await bot.fetch_channel(SERVER1_LEAVE_LOG_CHANNEL_ID)
            except Exception: leave_log_channel = None

        if leave_log_channel:
            now = datetime.datetime.now().strftime('%Y년 %m월 %d일 %H시 %M분')
            
            embed = discord.Embed(title="📤 유저 서버 퇴장", color=discord.Color.red())
            embed.add_field(name="👤 대상 유저", value=f"{member.name} (ID: {member.id})", inline=False)
            embed.add_field(name="⏰ 퇴장 시간", value=now, inline=False)
            
            if member.avatar: embed.set_image(url=member.avatar.url)
            else: embed.set_image(url=member.default_avatar.url)
                
            await leave_log_channel.send(embed=embed)

    # Server 2
    elif member.guild.id == SERVER2_ID:
        leave_log_channel = member.guild.get_channel(SERVER2_LEAVE_LOG_CHANNEL_ID)
        if not leave_log_channel:
            try: leave_log_channel = await bot.fetch_channel(SERVER2_LEAVE_LOG_CHANNEL_ID)
            except Exception: leave_log_channel = None

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

    if message.content.startswith(("! ", "!")):
        await bot.process_commands(message)
        return

    if message.content == "안녕":
        await message.channel.send(f"안녕하세요, {message.author.mention}님! 반가워요.")
        return

# ================= [ 관리자 명령어 로직 ] =================
@bot.command(name="인증완료")
@commands.has_permissions(manage_roles=True)
async def approve_auth(ctx, target: typing.Union[discord.Member, str], gender_input: str):
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    guild = ctx.guild

    # 1. 대상 결정 및 채널 결정 분기
    if guild.id == SERVER1_ID:
        target_channel_id = SERVER1_AUTH_CHANNEL_ID
    elif guild.id == SERVER2_ID:
        target_channel_id = SERVER2_AUTH_CHANNEL_ID
    else:
        target_channel_id = ctx.channel.id

    target_channel = guild.get_channel(target_channel_id)
    if not target_channel:
        target_channel = ctx.channel

    # 2. @everyone 전체 인증 처리 (서버 2 전용)
    if isinstance(target, str) and target in ["@everyone", "everyone"]:
        if guild.id != SERVER2_ID:
            await ctx.send("❌ 전체 인증 명령어는 서버 2에서만 사용 가능합니다.", delete_after=5)
            return

        # 성별 역할 이름 매핑
        if "남" in gender_input:
            gender_role_name = "【🟦】-『남자』"
        elif "여" in gender_input:
            gender_role_name = "【🟥】-『여자』"
        else:
            gender_role_name = gender_input

        # 역할 조회 및 생성
        gender_role = discord.utils.get(guild.roles, name=gender_role_name) or await guild.create_role(name=gender_role_name)
        member_role = discord.utils.get(guild.roles, name="【✅】-『멤버』") or await guild.create_role(name="【✅】-『멤버』")

        # 비동기 전체 지급 알림 및 처리 (대규모 인원 고려하여 안내 후 순차적 백그라운드 처리 권장)
        await target_channel.send(f"📢 모든 서버 멤버에게 **{member_role.name}** 및 **{gender_role.name}** 역할을 순차적으로 지급합니다. (전체 인증 프로세스 시작)")
        
        # 실제 백그라운드 순차 지급 진행
        for member in guild.members:
            if not member.bot:
                try:
                    await member.add_roles(member_role, gender_role)
                except discord.Forbidden:
                    continue
        return

    # 3. 개별 유저 인증 처리
    if not isinstance(target, discord.Member):
        await ctx.send("❌ 올바른 유저를 멘션하거나 @everyone을 입력해 주세요.", delete_after=5)
        return

    member = target

    # 서버별 성별 및 지급 역할 결정
    if guild.id == SERVER2_ID:
        if "남" in gender_input:
            gender_role_name = "【🟦】-『남자』"
        elif "여" in gender_input:
            gender_role_name = "【🟥】-『여자』"
        else:
            gender_role_name = gender_input

        gender_role = discord.utils.get(guild.roles, name=gender_role_name) or await guild.create_role(name=gender_role_name)
        member_role = discord.utils.get(guild.roles, name="【✅】-『멤버』") or await guild.create_role(name="【✅】-『멤버』")

        try:
            await member.add_roles(gender_role, member_role)
            await target_channel.send(f"🎉 축하합니다 {member.mention}님! 인증이 완료되었습니다.")
        except discord.Forbidden:
            await ctx.send("❌ 봇의 서열이 낮아 역할을 부여하지 못했습니다.", delete_after=5)

    else:
        # 기존 서버 1 및 기타 서버 로직 유지
        gender_role = None
        if gender_input.startswith("<@&") and gender_input.endswith(">"):
            role_id = int(gender_input.replace("<@&", "").replace(">", ""))
            gender_role = guild.get_role(role_id)
        else:
            gender_role = discord.utils.get(guild.roles, name=gender_input)

        if not gender_role:
            gender_role = await guild.create_role(name=gender_input)

        support_role = discord.utils.get(guild.roles, name="지원핑") or await guild.create_role(name="지원핑")
        end_role = discord.utils.get(guild.roles, name="end") or await guild.create_role(name="end")

        try:
            await member.add_roles(gender_role, support_role, end_role)
            await target_channel.send(f"🎉 축하합니다 {member.mention}님! 인증이 완료되었습니다.")
        except discord.Forbidden:
            await ctx.send("❌ 봇의 서열이 낮아 역할을 부여하지 못했습니다.", delete_after=5)

@bot.command(name="제재")
@commands.has_permissions(moderate_members=True) 
async def manual_punish(ctx, member: discord.Member, *, reason: str = "관리자 수동 제재"):
    try: await ctx.message.delete()
    except discord.Forbidden: pass
    await punish_user(ctx.guild, member, reason, str(ctx.author.name))

@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    guild_id = ctx.guild.id
    if guild_id not in SERVER_CONFIG:
        return

    log_channel = ctx.guild.get_channel(SERVER_CONFIG[guild_id])

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
            role = discord.utils.get(ctx.guild.roles, name=new_role_name) or await guild.create_role(name=new_role_name)
            await member.add_roles(role)
            action_description = f"타임아웃 해제 후 전과 단계를 하향했습니다. (**전과 {current_count}범** ➡️ **{new_role_name}**)"

        if log_channel:
            embed = discord.Embed(title="🔓 유저 제재 감면 및 해제", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 실행 관리자", value=str(ctx.author.name), inline=True)
            embed.description = action_description
            await log_channel.send(embed=embed)
            
    except discord.Forbidden:
        if log_channel: await log_channel.send("❌ 봇의 서열이 낮아 제재 감면 명령을 거부당했습니다.")
    except Exception as e:
        if log_channel: await log_channel.send(f"❌ 시스템 에러: `{e}`")

# ================= [ 실행 ] =================
keep_alive() 
bot.run(os.getenv("DISCORD_TOKEN"))