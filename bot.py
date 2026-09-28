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

# 서버 1 전용 인증방 설정
SERVER1_ID = 1553762662868058164
SERVER1_AUTH_CHANNEL_ID = 1554061009302585384

BANNED_WORDS = ["장애","애미","느금","너엄","너애미","너애비","느금마","느금빠","느개비","느그애비","애비","창녀","창년","보지","봊이","자지","섹스","섹x","정액"]

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
        
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove)
        await member.add_roles(role)

        punish_days = min(current_count, 28)
        duration = datetime.timedelta(days=punish_days)
        target_time = discord.utils.utcnow() + duration
        
        await member.edit(timed_out_until=target_time, reason=f"{role_name} 제재 ({reason})")

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

@bot.event
async def on_member_join(member: discord.Member):
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

# 명령어 인자 식별 오류 및 잘림 현상 조율 완료
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    # 1. !인증완료 명령어 직접 파싱 (우선순위 최고 단계 설정)
    if message.content.startswith("!인증완료"):
        # 서버 1의 지정된 인증 채널이 아니면 무시
        if message.channel.id != SERVER1_AUTH_CHANNEL_ID:
            return

        # '역할 관리' 권한이 있는 관리자만 사용 가능하게 보안 검증
        if not message.author.guild_permissions.manage_roles:
            return

        # 원본 명령어 메시지 즉시 삭제로 채널 보호
        try: await message.delete()
        except discord.Forbidden: pass

        # 인자 분석 (!인증완료 @유저 멘션 성별)
        args = message.content.split()
        if len(args) < 3:
            await message.channel.send("⚠️ 형식이 올바르지 않습니다. `!인증완료 @사용자 남자(또는 여자)` 형태로 입력해주세요.", delete_after=5)
            return

        gender = args[-1]  # 마지막 인자 ('남자' 또는 '여자')
        
        # 멘션 리스트에서 유저 객체 획득
        if not message.mentions:
            await message.channel.send("⚠️ 인증할 대상을 올바르게 멘션해 주세요.", delete_after=5)
            return
        
        target_member = message.mentions[0]  # 첫 번째 멘션된 유저를 타겟 지정

        # 서버 내 역할 검색
        role_jiwon = discord.utils.get(message.guild.roles, name="지원핑")
        role_end = discord.utils.get(message.guild.roles, name="end")
        role_man = discord.utils.get(message.guild.roles, name="남자")
        role_woman = discord.utils.get(message.guild.roles, name="여자")

        if not (role_jiwon and role_end and role_man and role_woman):
            await message.channel.send("⚠️ 서버 설정에 '남자', '여자', '지원핑', 'end' 역할이 모두 존재하는지 확인해 주세요.", delete_after=5)
            return

        roles_to_add = [role_jiwon, role_end]
        roles_to_remove = []

        # 성별에 따른 교정 및 교체 매커니즘
        if gender == "남자":
            roles_to_add.append(role_man)
            if role_woman in target_member.roles:
                roles_to_remove.append(role_woman)
        elif gender == "여자":
            roles_to_add.append(role_woman)
            if role_man in target_member.roles:
                roles_to_remove.append(role_man)
        else:
            await message.channel.send("⚠️ 성별은 '남자' 또는 '여자'로 입력해 주세요.", delete_after=5)
            return

        try:
            # 이전 성별 역할이 있다면 먼저 회수 진행
            if roles_to_remove:
                await target_member.remove_roles(*roles_to_remove)
            
            # 대상 유저에게 역할 일괄 세트 지급
            await target_member.add_roles(*roles_to_add)
            
            # 약속된 축하 안내 메시지 출력
            await message.channel.send(f"축하합니다! {target_member.mention}님의 인증이 완료되었어요!")
            return
            
        except discord.Forbidden:
            await message.channel.send("❌ 봇의 역할 서열이 낮습니다. 서버 설정 ➡️ 역할 메뉴에서 봇 역할을 관련 역할들보다 위로 올려주세요.")
            return
        except Exception as e:
            await message.channel.send(f"❌ 시스템 내부 오류 발생: `{e}`")
            return

    # 2. 일반 접두사 명령어 처리 우회
    if message.content.startswith("!"):
        await bot.process_commands(message)
        return

    # 3. 일반 채팅 안녕 반응 기능
    if message.content == "안녕":
        await message.channel.send(f"안녕하세요, {message.author.mention}님! 반가워요.")
        return

    # 4. 일반 채팅 자동 금지어 검열 시스템
    for word in BANNED_WORDS:
        if word in message.content:
            original_sentence = message.content
            
            try: await message.delete() 
            except discord.Forbidden: pass
            
            detailed_reason = f"금지어 `[{word}]` 사용 검열
**[적발된 문장 원본]**
|| {original_sentence} ||"
            
            await punish_user(message.guild, message.author, detailed_reason, "시스템 자동 검열")
            return

# !제재 @사용자 (사유) 명령어
@bot.command(name="제재")
@commands.has_permissions(moderate_members=True) 
async def manual_punish(ctx, member: discord.Member, *, reason: str = "관리자 수동 제재"):
    try: await ctx.message.delete()
    except discord.Forbidden: pass

    if ctx.author.top_role.position <= ctx.guild.me.top_role.position:
        return

    await punish_user(ctx.guild, member, reason, ctx.author.mention)

# !제재지우기 @사용자 명령어
@bot.command(name="제재지우기")
@commands.has_permissions(moderate_members=True)
async def clear_punish(ctx, member: discord.Member):
    try: await ctx.message.delete()
    except discord.Forbidden: pass

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
        await member.edit(timed_out_until=None, reason="관리자가 제재 감면 및 해제")

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