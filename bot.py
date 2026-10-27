import discord
from discord.ext import commands
from discord import app_commands
import datetime
import asyncio
from flask import Flask
from threading import Thread
import os
import json
import tempfile
import traceback
import io
import re
import aiohttp

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
# Render 등에서 Persistent Disk를 쓰면 DATA_DIR 환경변수에 마운트 경로(예: /data)를 넣어 주세요.
DATA_DIR = os.getenv("DATA_DIR", ".")
DATA_FILE = os.path.join(DATA_DIR, "guild_settings.json")
COMMAND_VERSION = 6   # 오래된 전역/서버 명령어 정리를 위해 버전을 올립니다.
META = {}

def load_settings():
    global META
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("_meta")
        META = meta if isinstance(meta, dict) else {}
        return {int(k): v for k, v in data.items() if str(k).isdigit()}
    except Exception as e:
        print(f"❌ 데이터 로드 중 오류 발생 (초기화 진행): {e}")
        return {}

def save_settings(settings):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data_to_save = {str(k): v for k, v in settings.items()}
        data_to_save["_meta"] = META
        dir_name = os.path.dirname(DATA_FILE) or '.'
        with tempfile.NamedTemporaryFile('w', dir=dir_name, delete=False, encoding='utf-8') as tf:
            json.dump(data_to_save, tf, ensure_ascii=False, indent=4)
            temp_name = tf.name
        os.replace(temp_name, DATA_FILE)
    except Exception as e:
        print(f"❌ 데이터 저장 중 치명적 오류 발생: {e}")

GUILD_SETTINGS = load_settings()

def get_gs(guild_id: int) -> dict:
    return GUILD_SETTINGS.setdefault(guild_id, {})

def enabled_keys(guild_id: int) -> set:
    return set(GUILD_SETTINGS.get(guild_id, {}).get("enabled", []))

def is_on(guild_id: int, key: str) -> bool:
    return key in GUILD_SETTINGS.get(guild_id, {}).get("enabled", [])

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class NattoBot(commands.Bot):
    async def setup_hook(self):
        # 전역 명령어로 기능 활성화 및 전체 활성화/비활성화를 등록합니다.
        self.tree.add_command(build_activation_group())
        self.tree.add_command(build_activate_all_command())
        self.tree.add_command(build_deactivate_all_command())
        # 이미 기능을 켜 둔 서버의 명령어는 (서버 쪽에 등록돼 있으므로) 로컬 트리에만 복원합니다.
        for gid, gs in list(GUILD_SETTINGS.items()):
            if gs.get("enabled"):
                register_guild_commands(gid)

bot = NattoBot(command_prefix="!", intents=intents)

# ================= [ 공통 헬퍼 ] =================
def format_member_name(member: discord.Member) -> str:
    """서버 닉네임(본 닉네임) 형태로 표기. 두 이름이 같으면 한 번만 표기."""
    server_nick = member.display_name
    original_nick = member.global_name or member.name
    def clean(text: str) -> str:
        return discord.utils.escape_mentions(discord.utils.escape_markdown(text))
    if server_nick == original_nick:
        return clean(server_nick)
    return f"{clean(server_nick)} ({clean(original_nick)})"

def josa(word: str, with_final: str, without_final: str) -> str:
    """마지막 글자의 받침 유무에 따라 조사를 선택합니다."""
    last = word.strip()[-1:]
    if "가" <= last <= "힣":
        return with_final if (ord(last) - 0xAC00) % 28 != 0 else without_final
    return without_final

def safe_text(text: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))

admin_only = app_commands.checks.has_permissions(administrator=True)

def setup_hint(guild_id: int, key: str) -> str:
    f = FEATURES[key]
    setup = f"`/설정 {key}`"
    if is_on(guild_id, key):
        return f"먼저 {setup} 명령어로 설정해 주세요."
    return f"먼저 `/활성화`에서 {key} 기능을 켠 뒤 {setup} 명령어로 설정해 주세요."

async def require_setting_channel(interaction: discord.Interaction, key: str, action: str) -> bool:
    """필수 채널 설정이 없으면 안내 문구를 남기고 False를 반환합니다."""
    guild = interaction.guild
    f = FEATURES[key]
    label = f["label"]
    channel_id = GUILD_SETTINGS.get(guild.id, {}).get(f["store"])
    if not channel_id:
        await interaction.response.send_message(
            f"⚠️ **{label}**{josa(label, '이', '가')} 설정되어 있지 않아 {action}{josa(action, '을', '를')} 실행할 수 없습니다.\n{setup_hint(guild.id, key)}",
            ephemeral=True
        )
        return False
    if guild.get_channel(channel_id) is None:
        await interaction.response.send_message(
            f"⚠️ 설정된 **{label}**{josa(label, '을', '를')} 찾을 수 없습니다. (채널이 삭제되었거나 봇이 볼 수 없는 채널입니다)\n{setup_hint(guild.id, key)}",
            ephemeral=True
        )
        return False
    return True

# ---------- ✅/❌ 반응으로 한 번 더 확인하는 공통 로직 ----------
CONFIRM_TIMEOUT = 60

async def confirm_by_reaction(interaction: discord.Interaction, warn_text: str, action: str) -> bool:
    """버튼으로 확인합니다. 반응 인텐트/이모지 권한에 의존하지 않습니다."""
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=CONFIRM_TIMEOUT)
            self.value = None

        async def interaction_check(self, button_interaction: discord.Interaction) -> bool:
            if button_interaction.user.id != interaction.user.id:
                await button_interaction.response.send_message(
                    "이 확인 버튼은 명령어를 실행한 사람만 사용할 수 있습니다.", ephemeral=True
                )
                return False
            return True

        @discord.ui.button(label="진행", style=discord.ButtonStyle.secondary, emoji="✅")
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            self.value = True
            await button_interaction.response.defer()
            self.stop()

        @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary, emoji="❌")
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            self.value = False
            await button_interaction.response.defer()
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(warn_text, view=view)
    try:
        await view.wait()
    except Exception as exc:
        print(f"확인 버튼 처리 오류: {exc}")
        try:
            await interaction.followup.send(
                f"❌ 확인 절차에서 오류가 발생해 {action}을(를) 취소했습니다. 기존 상태는 변경하지 않았습니다.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return False

    try:
        await interaction.edit_original_response(view=None)
    except discord.HTTPException:
        pass

    if view.value is True:
        return True
    if view.value is False:
        try:
            await interaction.followup.send(
                f"🚫 {action}이(가) 취소되었습니다. 기존 상태는 그대로 유지됩니다.", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return False

    try:
        await interaction.followup.send(
            f"⏰ {CONFIRM_TIMEOUT}초 동안 선택하지 않아 {action}이(가) 자동 취소되었습니다.",
            ephemeral=True,
        )
    except discord.HTTPException:
        pass
    return False

# ---------- 채널 설정 ----------
def make_set_channel(store: str, label: str):
    @app_commands.describe(채널=f"{label}에 사용할 텍스트 채널을 선택하세요.")
    @admin_only
    async def cb(interaction: discord.Interaction, 채널: discord.TextChannel):
        gs = get_gs(interaction.guild.id)
        gs[store] = 채널.id
        gs[f"{store}_name"] = 채널.name  # 채널이 삭제/교체됐을 때 복구할 수 있도록 이름도 저장
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"✅ {label}{josa(label, '이', '가')} {채널.mention}(으)로 설정되었습니다.", ephemeral=True)
    return cb

def make_remove_channel(store: str, label: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        gs = GUILD_SETTINGS.get(interaction.guild.id, {})
        if store in gs:
            del gs[store]
            gs.pop(f"{store}_name", None)
            save_settings(GUILD_SETTINGS)
            await interaction.response.send_message(f"🗑️ {label} 설정이 완전히 제거되었습니다.", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ 설정된 {label}{josa(label, '이', '가')} 없습니다.", ephemeral=True)
    return cb

def make_check_channel(store: str, label: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        channel_id = GUILD_SETTINGS.get(interaction.guild.id, {}).get(store)
        if not channel_id:
            await interaction.response.send_message(f"❌ 설정된 {label}{josa(label, '이', '가')} 없습니다.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(channel_id)
        channel_text = channel.mention if channel else "(삭제되었거나 접근할 수 없는 채널)"
        await interaction.response.send_message(f"📌 **현재 {label}:** {channel_text}\n🆔 채널 ID: `{channel_id}`", ephemeral=True)
    return cb

# ---------- 인증 지급 역할 ----------
@app_commands.describe(역할="지급할 기본 역할을 선택하세요. 여러 번 실행하면 다중 등록됩니다.")
@admin_only
async def set_auth_role_cb(interaction: discord.Interaction, 역할: discord.Role):
    if 역할 >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ 봇의 서열과 같거나 더 높은 역할은 자동 지급용으로 등록할 수 없습니다.", ephemeral=True)
        return
    role_ids = get_gs(interaction.guild.id).setdefault("auth_role_ids", [])
    if 역할.id not in role_ids:
        role_ids.append(역할.id)
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"✅ 기본 마스터 역할 목록에 **{safe_text(역할.name)}**이(가) 추가되었습니다.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ **{safe_text(역할.name)}**은(는) 이미 기본 역할 목록에 등록되어 있습니다.", ephemeral=True)

async def auth_role_autocomplete(interaction: discord.Interaction, current: str):
    guild = interaction.guild
    if not guild:
        return []
    cur = current.lower()
    choices = []
    for role_id in GUILD_SETTINGS.get(guild.id, {}).get("auth_role_ids", []):
        role = guild.get_role(role_id)
        label = role.name if role else f"(삭제된 역할) {role_id}"
        if cur and cur not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(role_id)))
    return choices[:25]

@app_commands.describe(역할="인증지급에서 제외할 역할을 목록에서 선택하세요.")
@app_commands.autocomplete(역할=auth_role_autocomplete)
@admin_only
async def remove_auth_role_cb(interaction: discord.Interaction, 역할: str):
    guild = interaction.guild
    role_ids = GUILD_SETTINGS.get(guild.id, {}).get("auth_role_ids", [])
    if not role_ids:
        await interaction.response.send_message("❌ 인증지급에 등록된 역할이 없습니다.", ephemeral=True)
        return
    if not 역할.isdigit() or int(역할) not in role_ids:
        await interaction.response.send_message("❌ 선택한 역할은 인증지급에 등록되어 있지 않습니다. 목록에서 선택해 주세요.", ephemeral=True)
        return
    role_id = int(역할)
    role_ids.remove(role_id)
    save_settings(GUILD_SETTINGS)
    role = guild.get_role(role_id)
    role_name = safe_text(role.name) if role else f"삭제된 역할({role_id})"
    await interaction.response.send_message(f"🗑️ **{role_name}** 역할이 인증지급에서 제외되었습니다. (남은 역할: {len(role_ids)}개)", ephemeral=True)

@admin_only
async def check_auth_role_cb(interaction: discord.Interaction):
    guild = interaction.guild
    role_ids = GUILD_SETTINGS.get(guild.id, {}).get("auth_role_ids", [])
    if not role_ids:
        await interaction.response.send_message("❌ 인증지급에 등록된 역할이 없습니다.", ephemeral=True)
        return
    lines = []
    for rid in role_ids:
        role = guild.get_role(rid)
        lines.append(role.mention if role else f"(삭제된 역할) `{rid}`")
    embed = discord.Embed(title=f"🎖️ 인증지급 역할 ({len(role_ids)}개)", description="\n".join(lines)[:4000], color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- 인증 안내 메세지 ----------
@app_commands.describe(메세지="인증방에 출력할 문구입니다. 줄바꿈은 \\n 을 입력하세요.")
@admin_only
async def set_auth_form_cb(interaction: discord.Interaction, 메세지: str):
    get_gs(interaction.guild.id)["auth_form"] = 메세지
    save_settings(GUILD_SETTINGS)
    await interaction.response.send_message("✅ 인증 안내 메세지가 정상적으로 저장되었습니다.", ephemeral=True)

@admin_only
async def check_auth_form_cb(interaction: discord.Interaction):
    form = GUILD_SETTINGS.get(interaction.guild.id, {}).get("auth_form")
    if not form:
        await interaction.response.send_message("❌ 등록된 인증 메세지가 없습니다.", ephemeral=True)
        return
    preview = form.replace("\\n", "\n")
    if len(preview) > 3900:
        preview = preview[:3900] + "\n…(일부 생략)"
    embed = discord.Embed(title="📝 등록된 인증 메세지", description=preview, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------- 제재/검열 단어 ----------
def make_set_words(key: str, label: str, done_text: str):
    @app_commands.describe(단어=f"{label}를 입력하세요. 여러 개는 쉼표(,)로 구분해 한 번에 넣을 수 있습니다.")
    @admin_only
    async def cb(interaction: discord.Interaction, 단어: str):
        words = get_gs(interaction.guild.id).setdefault(key, [])
        added = []
        for w in [w.strip() for w in 단어.split(",") if w.strip()]:
            # 중복 단어도 의도적으로 허용합니다.
            words.append(w)
            added.append(w)
        if not added:
            await interaction.response.send_message(f"⚠️ 등록할 {label}를 입력해 주세요.", ephemeral=True)
            return
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"{done_text} `{', '.join(added)}` 가 서버 필터에 누적 등록되었습니다.", ephemeral=True)
    return cb

def make_word_autocomplete(key: str):
    async def ac(interaction: discord.Interaction, current: str):
        if not interaction.guild:
            return []
        cur = current.lower()
        words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
        return [app_commands.Choice(name=w[:100], value=w) for w in words if cur in w.lower() and len(w) <= 100][:25]
    return ac

def make_remove_word(key: str, label: str):
    @app_commands.describe(단어=f"삭제할 {label}를 목록에서 선택하세요.")
    @app_commands.autocomplete(단어=make_word_autocomplete(key))
    @admin_only
    async def cb(interaction: discord.Interaction, 단어: str):
        words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
        if 단어 not in words:
            await interaction.response.send_message(f"❌ `{단어}` 은(는) 등록된 {label}가 아닙니다. 목록에서 선택해 주세요.", ephemeral=True)
            return
        words.remove(단어)
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(f"🗑️ {label} `{단어}` 이(가) 삭제되었습니다. (남은 단어: {len(words)}개)", ephemeral=True)
    return cb

def make_check_words(key: str, label: str, emoji: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        words = GUILD_SETTINGS.get(interaction.guild.id, {}).get(key, [])
        if not words:
            await interaction.response.send_message(f"❌ 등록된 {label}{josa(label, '이', '가')} 없습니다.", ephemeral=True)
            return
        word_text = ", ".join(f"`{w}`" for w in words)
        if len(word_text) > 3900:
            word_text = word_text[:3900] + " …(일부 생략)"
        embed = discord.Embed(title=f"{emoji} 등록된 {label} ({len(words)}개)", description=word_text, color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)
    return cb

# ---------- 목록 초기화 (✅/❌ 2차 확인) ----------
def make_reset(key: str, label: str):
    @admin_only
    async def cb(interaction: discord.Interaction):
        guild_id = interaction.guild.id
        items = GUILD_SETTINGS.get(guild_id, {}).get(key, [])
        if not items:
            await interaction.response.send_message(f"❌ 등록된 {label}{josa(label, '이', '가')} 없어 초기화할 내용이 없습니다.", ephemeral=True)
            return
        warn_text = (
            f"⚠️ **경고: {label} 전체 초기화**\n"
            f"{interaction.user.mention}님, 등록된 {label} **{len(items)}개**가 모두 삭제되며 되돌릴 수 없습니다.\n\n"
            f"✅ 를 누르면 **초기화 진행** / ❌ 를 누르면 **취소**\n"
            f"(명령어를 입력한 본인만 선택할 수 있으며, {CONFIRM_TIMEOUT}초 안에 선택하지 않으면 자동 취소됩니다.)"
        )
        if not await confirm_by_reaction(interaction, warn_text, f"{label} 초기화"):
            return
        gs = GUILD_SETTINGS.get(guild_id, {})
        if gs.get(key):
            gs[key] = []
            save_settings(GUILD_SETTINGS)
        try:
            await interaction.followup.send(f"🗑️ 등록된 {label} {len(items)}개가 모두 초기화되었습니다.", ephemeral=True)
        except discord.HTTPException:
            pass
    return cb

# ====================================================================
# 🛠️ 개별 기능 명령어 (/인증완료 /제재 /제재지우기 /추방 /추방해제 /청소 /서버청소 /가이드)
# ====================================================================

@app_commands.describe(유저="인증을 승인할 유저를 선택하세요.", 추가역할="성별이나 추가로 줄 특수 역할을 선택하세요. (선택사항)")
@app_commands.default_permissions(manage_roles=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
async def approve_user_cb(interaction: discord.Interaction, 유저: discord.Member, 추가역할: discord.Role = None):
    if not await require_setting_channel(interaction, "인증방", "인증 승인"):
        return
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
        await interaction.followup.send("❌ 지급할 기본 역할이나 추가역할이 없습니다. `/설정 인증 인증지급` 으로 먼저 등록해 주세요.")
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
            roles_text = ", ".join(f"**{safe_text(r.name)}**" for r in roles_to_add)
            await target_channel.send(f"🎉 축하합니다 {유저.mention}님! 서류 인증이 승인되어 멤버 역할({roles_text}) 자격이 활성화되었습니다.")
        await interaction.followup.send(f"✅ {safe_text(유저.name)}님에게 성공적으로 {len(roles_to_add)}개의 역할을 지급하고 승인했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 서열(역할 순위)이 타겟 유저 또는 지급 역할보다 낮아 처리에 실패했습니다.")

# ================= [ 공통 제재 및 전과 추가 로직 ] =================
async def punish_user(guild: discord.Guild, member: discord.Member, reason: str, punisher: str = "시스템 자동 검열") -> bool:
    """자동/수동 제재 공통 처리: 전과 역할, 누적 일수 타임아웃, 20범 영구 차단."""
    if not guild or not member or not guild.me:
        return False

    log_channel_id = GUILD_SETTINGS.get(guild.id, {}).get("punish_log_channel")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None

    async def send_log(**kwargs):
        if not log_channel:
            return
        try:
            await log_channel.send(**kwargs)
        except discord.HTTPException:
            pass

    if member.bot or member.id == guild.owner_id or member.top_role >= guild.me.top_role:
        embed = discord.Embed(title="❌ 제재 권한 실패 경고", color=discord.Color.dark_red())
        embed.description = f"{member.mention}님은 봇의 역할 순위 또는 대상 조건 때문에 제재할 수 없습니다."
        await send_log(embed=embed)
        return False

    current_count = 0
    roles_to_remove = []
    for role in member.roles:
        if role.name.startswith("전과 ") and role.name.endswith("범"):
            try:
                count = int(role.name.replace("전과 ", "").replace("범", "").strip())
                if count > current_count:
                    current_count = count
                roles_to_remove.append(role)
            except ValueError:
                continue
    current_count += 1

    try:
        if current_count >= 20:
            await member.ban(reason=f"전과 20범 달성 ({reason})"[:512])
            embed = discord.Embed(title="🚨 유저 영구 차단 (BAN)", color=discord.Color.red(), timestamp=discord.utils.utcnow())
            embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
            embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
            embed.add_field(name="💬 최종 사유", value=reason[:1024], inline=False)
            await send_log(embed=embed)
            return True

        punish_days = min(current_count, 28)
        await member.edit(
            timed_out_until=discord.utils.utcnow() + datetime.timedelta(days=punish_days),
            reason=f"전과 {current_count}범 제재 ({reason})"[:512],
        )

        role_name = f"전과 {current_count}범"
        role = discord.utils.get(guild.roles, name=role_name)
        if not role:
            role = await guild.create_role(name=role_name, reason="전과 시스템 자동 생성")
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="전과 단계 갱신")
        await member.add_roles(role, reason="전과 단계 갱신")

        embed = discord.Embed(title="⚠️ 유저 제재 알림", color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 대상자", value=f"{member.mention} ({member.name})", inline=True)
        embed.add_field(name="🔨 집행자", value=str(punisher), inline=True)
        embed.add_field(name="📜 조치 내용", value=f"**{role_name}** 부여 및 **{punish_days}일간** 타임아웃", inline=False)
        embed.add_field(name="💬 제재 사유", value=reason[:1024], inline=False)
        await send_log(embed=embed)
        return True
    except discord.Forbidden:
        await send_log(content=f"❌ **제재 권한 부족:** {member.mention}에게 타임아웃/역할 조치를 할 권한이 없습니다. 봇 역할 순위를 확인해 주세요.")
        return False
    except discord.HTTPException as exc:
        await send_log(content=f"❌ **제재 실행 실패:** `{str(exc)[:500]}`")
        return False


async def send_punish_log(guild: discord.Guild, embed: discord.Embed):
    """제재 로그 채널이 설정되어 있으면 기록합니다."""
    log_channel_id = GUILD_SETTINGS.get(guild.id, {}).get("punish_log_channel")
    log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
    if log_channel:
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException:
            pass


@app_commands.describe(멤버="제재할 멤버를 선택하세요.", 사유="제재 사유를 입력하세요.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def manual_punish_cb(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 제재"):
    if not await require_setting_channel(interaction, "제재로그", "제재"):
        return
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    display = format_member_name(멤버)

    if 멤버.top_role >= guild.me.top_role:
        await interaction.followup.send(f"❌ **제재 실패:** {display}(서열 서치 불가)")
        return

    ok = await punish_user(guild, 멤버, 사유, str(interaction.user.name))
    if ok:
        await interaction.followup.send(f"✅ **제재 완료:** {display}")
    else:
        await interaction.followup.send(f"❌ **제재 실패:** {display}(제재 실행 실패)")

@app_commands.describe(멤버="제재 복구 대상 멤버를 선택하세요.")
@app_commands.default_permissions(moderate_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def clear_punish_cb(interaction: discord.Interaction, 멤버: discord.Member):
    if not await require_setting_channel(interaction, "제재로그", "제재지우기"):
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    log_channel = guild.get_channel(GUILD_SETTINGS[guild.id]["punish_log_channel"])

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
        action_description = "대상자의 타임아웃 해제 및 전과 누적 단계를 초기화했습니다."

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

        await interaction.followup.send(f"🔓 {format_member_name(멤버)} 유저의 제재 스택 감면 집행을 완료했습니다.")
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇의 권한 등급(서열)이 멤버 권한보다 낮아 조치를 거부당했습니다.")
    except Exception as e:
        await interaction.followup.send(f"❌ 예외 에러 발생: {e}")

# ---------- 추방 = 영구 차단(밴) ----------
@app_commands.describe(멤버="추방(영구 차단)할 멤버를 선택하세요.", 사유="추방 사유를 입력하세요. (감사 로그와 제재 로그에 기록됩니다)")
@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def ban_member_cb(interaction: discord.Interaction, 멤버: discord.Member, 사유: str = "관리자 수동 추방"):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    display = format_member_name(멤버)

    if 멤버.id == interaction.user.id:
        await interaction.followup.send("❌ 자기 자신은 추방할 수 없습니다.")
        return
    if 멤버.id == guild.me.id:
        await interaction.followup.send("❌ 봇 자신은 추방할 수 없습니다.")
        return
    if 멤버.id == guild.owner_id:
        await interaction.followup.send("❌ 서버 소유자는 추방할 수 없습니다.")
        return
    if 멤버.top_role >= guild.me.top_role:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n봇의 역할 순위가 대상보다 낮거나 같습니다. 서버 설정에서 봇 역할을 위로 올려주세요.")
        return
    if interaction.user.id != guild.owner_id and 멤버.top_role >= interaction.user.top_role:
        await interaction.followup.send("❌ 자신과 같거나 더 높은 역할을 가진 멤버는 추방할 수 없습니다.")
        return

    try:
        await guild.ban(멤버, reason=f"{사유} (집행자: {interaction.user.name})"[:512])
    except discord.Forbidden:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n봇에게 '멤버 차단하기' 권한이 없거나 역할 순위가 부족합니다.")
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ **추방 실패:** {display}\n`{e}`")
        return

    safe_reason = discord.utils.escape_mentions(사유)
    await interaction.followup.send(f"🔨 **추방(영구 차단) 완료:** {display}\n📝 **사유:** {safe_reason}\n다시 들어오려면 `/추방해제` 가 필요합니다.")

    embed = discord.Embed(title="🔨 유저 추방(영구 차단) 알림", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="👤 대상자", value=f"{멤버.mention} ({멤버.name})", inline=True)
    embed.add_field(name="🔨 집행자", value=str(interaction.user.name), inline=True)
    embed.add_field(name="💬 추방 사유", value=safe_reason[:1024], inline=False)
    await send_punish_log(guild, embed)

# ---------- 추방해제: 차단 목록에서 선택 ----------
class UnbanView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, invoker_id: int, entries: list):
        super().__init__(timeout=180)
        self.invoker_id = invoker_id
        self.entries = entries
        self.page = 0
        self.interaction = None
        self._render()

    @property
    def last_page(self) -> int:
        return max(0, (len(self.entries) - 1) // self.PAGE_SIZE)

    def make_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔓 추방 해제",
            description=f"이 서버에서 차단된 유저 **{len(self.entries)}명**\n아래 목록에서 해제할 유저를 선택하세요.",
            color=discord.Color.green()
        )
        embed.set_footer(text=f"{self.page + 1} / {self.last_page + 1} 페이지 · 선택하면 즉시 차단이 해제됩니다")
        return embed

    def _render(self):
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        chunk = self.entries[start:start + self.PAGE_SIZE]
        options = [
            discord.SelectOption(label=(e["label"][:100] or str(e["id"])), value=str(e["id"]), description=e["desc"][:100])
            for e in chunk
        ]
        select = discord.ui.Select(placeholder="🔓 해제할 유저를 선택하세요", options=options, row=0)
        select.callback = self.on_select
        self.add_item(select)
        if self.last_page > 0:
            prev_btn = discord.ui.Button(label="◀ 이전", style=discord.ButtonStyle.secondary, disabled=self.page == 0, row=1)
            next_btn = discord.ui.Button(label="다음 ▶", style=discord.ButtonStyle.secondary, disabled=self.page >= self.last_page, row=1)
            prev_btn.callback = self.on_prev
            next_btn.callback = self.on_next
            self.add_item(prev_btn)
            self.add_item(next_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("❌ 명령어를 입력한 본인만 선택할 수 있습니다.", ephemeral=True)
            return False
        return True

    async def on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_next(self, interaction: discord.Interaction):
        self.page = min(self.last_page, self.page + 1)
        self._render()
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def on_select(self, interaction: discord.Interaction):
        uid = int(interaction.data["values"][0])
        entry = next((e for e in self.entries if e["id"] == uid), None)
        name = safe_text(entry["label"]) if entry else str(uid)
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=f"관리자 수동 차단 해제 (집행자: {interaction.user.name})"[:512])
        except discord.NotFound:
            await interaction.response.edit_message(content=f"⚠️ **{name}** 님은 이미 차단 목록에 없습니다.", embed=None, view=None)
            self.stop()
            return
        except discord.Forbidden:
            await interaction.response.edit_message(content="❌ 봇에게 '멤버 차단하기' 권한이 없어 해제할 수 없습니다.", embed=None, view=None)
            self.stop()
            return
        except discord.HTTPException as e:
            await interaction.response.edit_message(content=f"❌ 차단 해제 중 오류가 발생했습니다: `{e}`", embed=None, view=None)
            self.stop()
            return

        await interaction.response.edit_message(content=f"✅ **추방 해제 완료:** {name}\n이제 다시 서버에 들어올 수 있습니다.", embed=None, view=None)
        self.stop()

        embed = discord.Embed(title="🔓 유저 추방 해제 알림", color=discord.Color.green(), timestamp=discord.utils.utcnow())
        embed.add_field(name="👤 대상자", value=f"{name} (ID: {uid})", inline=True)
        embed.add_field(name="🔨 집행자", value=str(interaction.user.name), inline=True)
        await send_punish_log(interaction.guild, embed)

    async def on_timeout(self):
        if self.interaction:
            try:
                await self.interaction.edit_original_response(content="⏰ 시간이 초과되었습니다. `/추방해제` 를 다시 입력해 주세요.", embed=None, view=None)
            except discord.HTTPException:
                pass

@app_commands.default_permissions(ban_members=True)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
async def unban_member_cb(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    try:
        bans = [entry async for entry in guild.bans(limit=None)]
    except discord.Forbidden:
        await interaction.followup.send("❌ 봇에게 '멤버 차단하기' 권한이 없어 차단 목록을 불러올 수 없습니다.")
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ 차단 목록을 불러오지 못했습니다: `{e}`")
        return

    if not bans:
        await interaction.followup.send("✅ 이 서버에는 차단된 유저가 없습니다.")
        return

    entries = []
    for be in bans:
        u = be.user
        shown = u.global_name or u.name
        label = f"{shown} ({u.name})" if shown != u.name else u.name
        reason = (be.reason or "사유 없음").replace("\n", " ")
        entries.append({"id": u.id, "label": label, "desc": f"ID {u.id} · {reason}"})
    entries.sort(key=lambda e: e["label"].lower())

    view = UnbanView(interaction.user.id, entries)
    await interaction.followup.send(embed=view.make_embed(), view=view)
    view.interaction = interaction


# ---------- 청소 / 서버청소 ----------
RUNNING_CLEANS = set()   # 서버별 동시 청소 방지

def _is_category_channel(channel):
    return isinstance(channel, discord.CategoryChannel)

def _channel_kind(channel):
    """채널 복제에 지원하는 채널 유형을 반환합니다."""
    if isinstance(channel, discord.CategoryChannel):
        return "category"
    if hasattr(discord, "ForumChannel") and isinstance(channel, discord.ForumChannel):
        return "forum"
    if hasattr(discord, "StageChannel") and isinstance(channel, discord.StageChannel):
        return "stage"
    if hasattr(discord, "NewsChannel") and isinstance(channel, discord.NewsChannel):
        return "news"
    if isinstance(channel, discord.TextChannel):
        return "text"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    return None

async def _snapshot_message(message, guild):
    """메시지 내용/임베드/첨부파일을 원본 채널 삭제 전에 메모리에 복사합니다."""
    files = []
    for attachment in message.attachments:
        limit = getattr(guild, "filesize_limit", 8 * 1024 * 1024)
        if attachment.size > limit:
            raise RuntimeError(
                f"첨부파일 '{attachment.filename}'이 서버 업로드 제한({limit}바이트)을 초과합니다."
            )
        data = await attachment.read()
        files.append({
            "filename": attachment.filename,
            "description": getattr(attachment, "description", None),
            "data": data,
            "spoiler": attachment.is_spoiler(),
        })
    return {
        "content": message.content or None,
        "embeds": [embed.to_dict() for embed in message.embeds],
        "files": files,
        "was_pinned": bool(getattr(message, "pinned", False)),
        "created_at": message.created_at,
    }

async def _send_message_snapshot(destination, snapshot, *, pin=False):
    files = [
        discord.File(
            io.BytesIO(item["data"]),
            filename=item["filename"],
            description=item["description"],
            spoiler=item["spoiler"],
        )
        for item in snapshot.get("files", [])
    ]
    embeds = [discord.Embed.from_dict(item) for item in snapshot.get("embeds", [])[:10]]
    content = snapshot.get("content")
    if not content and not embeds and not files:
        content = "‎"  # 빈 메시지 형태의 고정 메시지도 복원할 수 있도록 보이지 않는 문자 사용
    try:
        message = await destination.send(
            content=content,
            embeds=embeds,
            files=files,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        for file in files:
            try:
                file.close()
            except Exception:
                pass
        raise
    if pin:
        await message.pin(reason="낫또봇 채널 청소: 기존 고정 메시지 복원")
    return message

async def _get_forum_threads(forum):
    """포럼 채널의 활성/보관 게시글을 모읍니다."""
    threads = {thread.id: thread for thread in getattr(forum, "threads", [])}
    try:
        async for thread in forum.archived_threads(limit=None):
            threads[thread.id] = thread
    except discord.HTTPException as exc:
        raise RuntimeError(f"포럼 게시글 목록을 읽지 못했습니다: {exc}") from exc
    return list(threads.values())

async def _snapshot_forum_posts(forum, guild):
    """고정 메시지가 있는 포럼 게시글과 그 안의 고정 메시지를 백업합니다."""
    posts = []
    for thread in await _get_forum_threads(forum):
        try:
            pinned = await thread.pins()
        except discord.HTTPException as exc:
            raise RuntimeError(f"포럼 게시글 '{thread.name}'의 고정 메시지를 읽지 못했습니다: {exc}") from exc
        flags = getattr(thread, "flags", None)
        thread_is_pinned = bool(
            getattr(thread, "pinned", False) or getattr(flags, "pinned", False)
        )
        if not pinned and not thread_is_pinned:
            continue
        try:
            starter = await thread.fetch_message(thread.id)
        except discord.HTTPException as exc:
            raise RuntimeError(f"포럼 게시글 '{thread.name}'의 원본 게시글을 읽지 못했습니다: {exc}") from exc
        starter_snapshot = await _snapshot_message(starter, guild)
        pinned_ids = {message.id for message in pinned}
        replies = []
        for message in sorted(pinned, key=lambda m: m.created_at):
            if message.id == starter.id:
                continue
            snap = await _snapshot_message(message, guild)
            snap["was_pinned"] = True
            replies.append(snap)
        posts.append({
            "name": thread.name,
            "starter": starter_snapshot,
            "starter_pinned": starter.id in pinned_ids,
            "thread_pinned": thread_is_pinned,
            "replies": replies,
            "tag_names": [tag.name for tag in getattr(thread, "applied_tags", [])],
            "auto_archive_duration": getattr(thread, "auto_archive_duration", 1440),
            "locked": bool(getattr(thread, "locked", False)),
        })
    return posts

async def _snapshot_channel(channel, guild):
    kind = _channel_kind(channel)
    if kind is None:
        raise RuntimeError(
            f"지원하지 않는 채널 유형({getattr(channel, 'type', '알 수 없음')}): "
            f"'{getattr(channel, 'name', '알 수 없음')}'. 안전을 위해 아무 채널도 삭제하지 않았습니다."
        )
    snap = {
        "old": channel,
        "id": channel.id,
        "name": channel.name,
        "kind": kind,
        "position": getattr(channel, "position", 0),
        "category_id": getattr(getattr(channel, "category", None), "id", None),
        "overwrites": dict(getattr(channel, "overwrites", {})),
        "permissions_synced": bool(getattr(channel, "permissions_synced", False)),
    }
    if kind in ("text", "news"):
        snap.update({
            "topic": getattr(channel, "topic", None),
            "slowmode_delay": getattr(channel, "slowmode_delay", 0),
            "nsfw": getattr(channel, "nsfw", False),
            "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", 1440),
            "default_thread_slowmode_delay": getattr(channel, "default_thread_slowmode_delay", 0),
        })
        pins = await channel.pins()
        snap["pins"] = [
            await _snapshot_message(message, guild)
            for message in sorted(pins, key=lambda m: m.created_at)
        ]
    elif kind == "forum":
        snap.update({
            "topic": getattr(channel, "topic", None),
            "slowmode_delay": getattr(channel, "slowmode_delay", 0),
            "nsfw": getattr(channel, "nsfw", False),
            "default_auto_archive_duration": getattr(channel, "default_auto_archive_duration", 1440),
            "default_thread_slowmode_delay": getattr(channel, "default_thread_slowmode_delay", 0),
            "sort_order": getattr(channel, "sort_order", None),
            "layout": getattr(channel, "layout", None),
            "available_tags": list(getattr(channel, "available_tags", [])),
            "forum_posts": await _snapshot_forum_posts(channel, guild),
        })
    elif kind in ("voice", "stage"):
        snap.update({
            "bitrate": getattr(channel, "bitrate", None),
            "user_limit": getattr(channel, "user_limit", 0),
            "rtc_region": getattr(channel, "rtc_region", None),
            "video_quality_mode": getattr(channel, "video_quality_mode", None),
        })
    return snap

def _clone_forum_tags(tags):
    cloned = []
    for tag in tags:
        kwargs = {"name": tag.name, "moderated": getattr(tag, "moderated", False)}
        emoji = getattr(tag, "emoji", None)
        if emoji is not None:
            kwargs["emoji"] = emoji
        cloned.append(discord.ForumTag(**kwargs))
    return cloned

async def _create_channel_copy(guild, snap, category=None):
    kind = snap["kind"]
    name = snap["name"]
    overwrites = snap["overwrites"]
    reason = "낫또봇 채널 청소: 기존 채널 설정 복원"
    if kind == "category":
        new_channel = await guild.create_category(
            name, overwrites=overwrites, reason=reason
        )
    elif kind in ("text", "news"):
        new_channel = await guild.create_text_channel(
            name,
            category=category,
            overwrites=overwrites,
            topic=snap.get("topic"),
            slowmode_delay=snap.get("slowmode_delay", 0),
            nsfw=snap.get("nsfw", False),
            default_auto_archive_duration=snap.get("default_auto_archive_duration", 1440),
            default_thread_slowmode_delay=snap.get("default_thread_slowmode_delay", 0),
            news=(kind == "news"),
            reason=reason,
        )
    elif kind == "voice":
        kwargs = {
            "category": category,
            "overwrites": overwrites,
            "user_limit": snap.get("user_limit", 0),
            "reason": reason,
        }
        if snap.get("bitrate") is not None:
            kwargs["bitrate"] = snap["bitrate"]
        if snap.get("rtc_region") is not None:
            kwargs["rtc_region"] = snap["rtc_region"]
        if snap.get("video_quality_mode") is not None:
            kwargs["video_quality_mode"] = snap["video_quality_mode"]
        new_channel = await guild.create_voice_channel(name, **kwargs)
    elif kind == "stage":
        kwargs = {
            "category": category,
            "overwrites": overwrites,
            "user_limit": snap.get("user_limit", 0),
            "reason": reason,
        }
        if snap.get("bitrate") is not None:
            kwargs["bitrate"] = snap["bitrate"]
        if snap.get("rtc_region") is not None:
            kwargs["rtc_region"] = snap["rtc_region"]
        if snap.get("video_quality_mode") is not None:
            kwargs["video_quality_mode"] = snap["video_quality_mode"]
        new_channel = await guild.create_stage_channel(name, **kwargs)
    elif kind == "forum":
        kwargs = {
            "category": category,
            "overwrites": overwrites,
            "topic": snap.get("topic"),
            "slowmode_delay": snap.get("slowmode_delay", 0),
            "nsfw": snap.get("nsfw", False),
            "default_auto_archive_duration": snap.get("default_auto_archive_duration", 1440),
            "default_thread_slowmode_delay": snap.get("default_thread_slowmode_delay", 0),
            "available_tags": _clone_forum_tags(snap.get("available_tags", [])),
            "reason": reason,
        }
        if snap.get("sort_order") is not None:
            kwargs["sort_order"] = snap["sort_order"]
        if snap.get("layout") is not None:
            kwargs["layout"] = snap["layout"]
        new_channel = await guild.create_forum_channel(name, **kwargs)
    else:
        raise RuntimeError(f"지원하지 않는 채널 유형: {kind}")
    if snap.get("permissions_synced") and category is not None:
        await new_channel.edit(sync_permissions=True, reason=reason)
    return new_channel

async def _restore_forum_posts(new_forum, posts):
    restored = 0
    tags_by_name = {tag.name: tag for tag in getattr(new_forum, "available_tags", [])}
    for post in posts:
        starter = post["starter"]
        files = [
            discord.File(
                io.BytesIO(item["data"]),
                filename=item["filename"],
                description=item["description"],
                spoiler=item["spoiler"],
            )
            for item in starter.get("files", [])
        ]
        embeds = [discord.Embed.from_dict(item) for item in starter.get("embeds", [])[:10]]
        kwargs = {
            "name": post["name"],
            "content": starter.get("content") or "‎",
            "embeds": embeds,
            "files": files,
            "applied_tags": [
                tags_by_name[name] for name in post.get("tag_names", []) if name in tags_by_name
            ],
            "auto_archive_duration": post.get("auto_archive_duration", 1440),
            "allowed_mentions": discord.AllowedMentions.none(),
            "reason": "낫또봇 채널 청소: 기존 포럼 게시글 복원",
        }
        created = await new_forum.create_thread(**kwargs)
        new_thread = getattr(created, "thread", created)
        starter_message = getattr(created, "message", None)
        if starter_message is None:
            starter_message = await new_thread.fetch_message(new_thread.id)
        if post.get("starter_pinned"):
            await starter_message.pin(reason="낫또봇 채널 청소: 고정 포스트 복원")
        if post.get("thread_pinned"):
            try:
                await new_thread.edit(pinned=True, reason="낫또봇 채널 청소: 고정 포스트 복원")
            except (discord.HTTPException, TypeError):
                pass
        for reply in post.get("replies", []):
            await _send_message_snapshot(new_thread, reply, pin=True)
        if post.get("locked"):
            try:
                await new_thread.edit(locked=True, archived=True, reason="낫또봇 채널 청소: 게시글 상태 복원")
            except discord.HTTPException:
                pass
        restored += 1
    return restored

async def _restore_channel_pins(new_channel, snap):
    restored = 0
    if snap["kind"] in ("text", "news"):
        for message_snapshot in snap.get("pins", []):
            await _send_message_snapshot(new_channel, message_snapshot, pin=True)
            restored += 1
    elif snap["kind"] == "forum":
        restored += await _restore_forum_posts(new_channel, snap.get("forum_posts", []))
    return restored

def _remap_saved_channel_ids(guild_id, channel_map):
    """봇 자체 설정에 저장된 채널 ID를 새 채널 ID로 바꿉니다."""
    gs = GUILD_SETTINGS.get(guild_id)
    if not isinstance(gs, dict):
        return
    changed = False
    for key, value in list(gs.items()):
        if isinstance(value, int) and not isinstance(value, bool) and value in channel_map:
            if "channel" in key.lower() or key in ("auth_channel", "punish_log_channel", "join_log_channel", "leave_log_channel"):
                gs[key] = channel_map[value]
                changed = True
    if changed:
        save_settings(GUILD_SETTINGS)

async def _delete_created_channels(created):
    for channel in reversed(created):
        try:
            await channel.delete(reason="낫또봇 청소 복구: 복제 실패로 생성한 채널 정리")
        except discord.HTTPException:
            pass

async def _recreate_one_channel(interaction, channel):
    guild = interaction.guild
    if _channel_kind(channel) is None or _channel_kind(channel) == "category":
        raise RuntimeError("텍스트/공지/음성/스테이지/포럼 채널에서만 /청소를 사용할 수 있습니다.")
    if not guild.me.guild_permissions.manage_channels:
        raise RuntimeError("봇에게 채널 관리 권한이 필요합니다.")
    if not channel.permissions_for(guild.me).manage_channels:
        raise RuntimeError("봇에게 이 채널의 채널 관리 권한이 없습니다.")

    snap = await _snapshot_channel(channel, guild)
    category = channel.category
    new_channel = None
    try:
        new_channel = await _create_channel_copy(guild, snap, category=category)
        await _restore_channel_pins(new_channel, snap)
        await channel.delete(reason=f"/청소 (집행자: {interaction.user})")
        try:
            await new_channel.edit(position=snap["position"], reason="낫또봇 채널 청소: 채널 순서 복원")
        except discord.HTTPException:
            pass
        _remap_saved_channel_ids(guild.id, {channel.id: new_channel.id})
        return new_channel
    except Exception:
        if new_channel is not None and channel.id in {c.id for c in guild.channels}:
            try:
                await new_channel.delete(reason="낫또봇 청소 취소: 복원 실패")
            except discord.HTTPException:
                pass
        raise

@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def clean_channel_cb(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    if guild.id in RUNNING_CLEANS:
        await interaction.response.send_message("⏳ 이 서버에서 이미 채널 정리가 진행 중입니다.", ephemeral=True)
        return
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)) and not (
        hasattr(discord, "ForumChannel") and isinstance(channel, discord.ForumChannel)
    ):
        await interaction.response.send_message("❌ 일반 채널·공지 채널·음성·스테이지·포럼 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    warn_text = (
        f"🧹 **경고: 채널 재생성**\n"
        f"{interaction.user.mention}님, {channel.mention} 채널을 새로 만들고 기존 채널을 삭제합니다.\n"
        f"채널 권한·설정·위치와 고정 메시지/고정 포스트를 복사합니다. 일반 메시지는 복사되지 않습니다.\n"
        f"채널 ID는 바뀌지만, 낫또봇의 저장된 채널 설정은 새 ID로 갱신합니다.\n\n"
        f"✅ 진행 / ❌ 취소 (본인만 선택 가능, {CONFIRM_TIMEOUT}초 후 자동 취소)"
    )
    if not await confirm_by_reaction(interaction, warn_text, "채널 재생성"):
        return

    RUNNING_CLEANS.add(guild.id)
    try:
        new_channel = await _recreate_one_channel(interaction, channel)
        await interaction.followup.send(
            f"✅ 채널을 재생성했습니다: {new_channel.mention}\n"
            f"채널 설정과 고정 메시지/포스트를 복원했습니다. 일반 메시지는 복원하지 않았습니다.",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(
            f"❌ 채널 재생성에 실패했습니다. 기존 채널이 남아 있는지 확인해 주세요.\n`{str(exc)[:1200]}`",
            ephemeral=True,
        )
    finally:
        RUNNING_CLEANS.discard(guild.id)

async def collect_all_threads(guild: discord.Guild) -> dict:
    """활성 스레드와 접근 가능한 보관 스레드/포럼 게시글을 수집합니다."""
    found = {thread.id: thread for thread in getattr(guild, "threads", [])}
    try:
        for thread in await guild.active_threads():
            found[thread.id] = thread
    except (discord.HTTPException, AttributeError):
        pass

    for channel in guild.channels:
        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            continue
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            continue
        try:
            async for thread in channel.archived_threads(limit=None):
                found[thread.id] = thread
            if isinstance(channel, discord.TextChannel) and perms.manage_threads:
                async for thread in channel.archived_threads(limit=None, private=True):
                    found[thread.id] = thread
        except (discord.HTTPException, AttributeError):
            # 접근할 수 없는 보관 스레드는 아래 건너뜀 통계에 포함됩니다.
            continue
    return found

@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def clean_server_cb(interaction: discord.Interaction):
    """서버의 채널을 복제하고 고정 메시지/포스트를 복원한 뒤 원본 채널을 삭제합니다."""
    guild = interaction.guild
    if guild.id in RUNNING_CLEANS:
        await interaction.response.send_message(
            "⏳ 이 서버에서 이미 청소가 진행 중입니다. 끝난 뒤 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    warn_text = (
        f"🧨 **경고: 서버 전체 채널 재생성**\n"
        f"{interaction.user.mention}님, 서버의 카테고리와 채널을 새로 만들고 기존 채널을 삭제합니다.\n"
        f"채널 설정·권한·순서, 낫또봇의 입장로그 등 저장된 채널 설정을 새 채널 ID로 옮깁니다.\n"
        f"텍스트 채널의 고정 메시지와 포럼의 고정 게시글을 복원합니다. 일반 메시지와 일반 스레드는 복원되지 않습니다.\n"
        f"⚠️ 작업 도중 복제/복원이 실패하면 원본 채널은 삭제하지 않고 새로 만든 복제본을 정리합니다.\n\n"
        f"✅ **진행** 버튼으로 실행 / ❌ **취소** 버튼으로 취소 "
        f"(본인만 선택 가능, {CONFIRM_TIMEOUT}초 후 자동 취소)"
    )
    if not await confirm_by_reaction(interaction, warn_text, "서버 전체 채널 재생성"):
        return

    RUNNING_CLEANS.add(guild.id)
    created = []
    snapshots = []
    channel_map = {}
    originals_deleted = 0
    try:
        try:
            await interaction.followup.send(
                "🧹 서버 전체 채널을 백업하고 복제합니다. 원본 채널은 복제가 끝나기 전까지 삭제하지 않습니다.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

        # 1) 먼저 모든 채널 설정과 고정 콘텐츠를 메모리에 저장합니다.
        original_channels = list(guild.channels)
        for channel in original_channels:
            snapshots.append(await _snapshot_channel(channel, guild))

        # 2) 카테고리를 먼저 만들고, 그다음 나머지 채널을 원래 카테고리에 연결합니다.
        ordered_snapshots = (
            [snap for snap in snapshots if snap["kind"] == "category"]
            + [snap for snap in snapshots if snap["kind"] != "category"]
        )
        for snap in ordered_snapshots:
            old_category_id = snap.get("category_id")
            new_category = channel_map.get(old_category_id) if old_category_id else None
            new_channel = await _create_channel_copy(guild, snap, category=new_category)
            created.append(new_channel)
            channel_map[snap["id"]] = new_channel

        # 3) 복제본이 전부 만들어진 뒤 고정 메시지와 고정 포럼 게시글을 복원합니다.
        for snap in snapshots:
            if snap["kind"] == "category":
                continue
            await _restore_channel_pins(channel_map[snap["id"]], snap)

        # 4) 모든 복제와 복원이 성공한 경우에만 원본을 삭제합니다.
        delete_order = (
            [snap for snap in reversed(snapshots) if snap["kind"] != "category"]
            + [snap for snap in reversed(snapshots) if snap["kind"] == "category"]
        )
        delete_errors = []
        for snap in delete_order:
            old_channel = snap["old"]
            try:
                await old_channel.delete(reason=f"/서버청소 (집행자: {interaction.user})")
                originals_deleted += 1
            except discord.HTTPException as exc:
                delete_errors.append(f"{getattr(old_channel, 'name', snap['id'])}: {exc}")

        # 5) 카테고리 순서와 각 카테고리 안의 채널 순서를 따로 복원합니다.
        # 전체 채널을 position 하나로 정렬하면 음성/텍스트 채널과 카테고리 순서가 섞일 수 있습니다.
        position_reason = "낫또봇 서버청소: 원래 채널 순서 복원"
        category_snaps = sorted(
            (snap for snap in snapshots if snap["kind"] == "category"),
            key=lambda item: item.get("position", 0),
        )
        for position, snap in enumerate(category_snaps):
            try:
                await channel_map[snap["id"]].edit(
                    position=position, reason=position_reason
                )
            except (discord.HTTPException, AttributeError):
                pass

        # 카테고리 밖 채널과 각 카테고리 내부 채널을 각각 원래 순서대로 정렬합니다.
        parent_ids = [None] + [snap["id"] for snap in category_snaps]
        for parent_id in parent_ids:
            siblings = sorted(
                (
                    snap for snap in snapshots
                    if snap["kind"] != "category"
                    and snap.get("category_id") == parent_id
                ),
                key=lambda item: item.get("position", 0),
            )
            for position, snap in enumerate(siblings):
                try:
                    await channel_map[snap["id"]].edit(
                        position=position, reason=position_reason
                    )
                except (discord.HTTPException, AttributeError):
                    pass

        _remap_saved_channel_ids(guild.id, {old_id: new.id for old_id, new in channel_map.items()})

        embed = discord.Embed(
            title="🧹 서버 전체 청소 완료",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="📦 복제한 채널/카테고리", value=f"{len(created)}개", inline=True)
        embed.add_field(name="🗑️ 삭제한 원본 채널/카테고리", value=f"{originals_deleted}개", inline=True)
        embed.add_field(
            name="🔒 설정",
            value="채널 설정·권한·순서 및 낫또봇의 저장된 입장로그 등 채널 ID 설정을 새 ID로 갱신했습니다.",
            inline=False,
        )
        embed.add_field(
            name="📌 콘텐츠",
            value="고정 메시지와 고정 포럼 게시글을 복원했습니다. 일반 메시지와 일반 스레드는 복원하지 않습니다.",
            inline=False,
        )
        if delete_errors:
            embed.add_field(
                name="⚠️ 삭제하지 못한 원본",
                value="일부 원본 채널은 권한 또는 Discord 오류로 남아 있을 수 있습니다. 중복 채널을 확인해 주세요.\n"
                      + "\n".join(f"• {err}" for err in delete_errors[:8])[:1000],
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as exc:
        # 복제 또는 콘텐츠 복원 단계에서 실패했다면 원본은 아직 삭제하지 않았으므로 복제본만 정리합니다.
        if originals_deleted == 0 and created:
            await _delete_created_channels(created)
        try:
            status = (
                "원본 채널은 삭제하지 않았으며 생성한 복제본을 정리했습니다.\n"
                if originals_deleted == 0 else
                f"원본 채널 {originals_deleted}개가 이미 삭제되었을 수 있으니 서버 채널 목록을 확인해 주세요.\n"
            )
            await interaction.followup.send(
                "❌ 서버청소 중 오류가 발생했습니다. " + status
                + f"`{type(exc).__name__}: {str(exc)[:900]}`",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
    finally:
        RUNNING_CLEANS.discard(guild.id)

# ---------- 가이드 ----------
CATEGORY_WARN = {
    "인증": "`인증방`·`인증지급`·`인증메세지` 중 하나라도 비어 있으면 신입에게 안내가 안 뜨거나 역할을 못 줘서 **가입 단계가 멈춥니다.** (`인증방`이 없으면 `/인증완료`도 실행되지 않아요.)",
    "제재": "`제재로그`가 비어 있으면 **`/제재`·`/제재지우기`를 쓸 수 없고**, 자동 제재는 처리돼도 로그가 남지 않습니다.",
    "입퇴장": "로그 채널을 정하지 않으면 해당 입장/퇴장 기록이 남지 않습니다.",
    "청소": "삭제는 **되돌릴 수 없습니다.** `/서버청소`는 채널·카테고리·입장로그 설정을 유지하지만 메시지·스레드·포럼 게시글은 삭제합니다.",
    "기타": "",
}

def build_guide_embed(guild_id: int, page: str) -> discord.Embed:
    enabled = enabled_keys(guild_id)
    embed = discord.Embed(color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.set_footer(text="안전하고 청정한 서버 환경을 구축합니다 • Powered by NattoBot")

    if page == "home":
        embed.title = "🛡️ 낫또봇 가이드"
        embed.description = "아래 메뉴에서 카테고리를 골라 각 명령어의 사용법과 설정 방법을 확인하세요."
        embed.add_field(name="⚙️ 기능 활성화", value="`/활성화`에서 기능을 선택하거나 `/모두활성화`로 전체 기능을 켭니다. `/모두비활성화`는 전체 기능을 끕니다.", inline=False)
        embed.add_field(name="🛠️ 설정 및 관리", value="`/설정`은 항목에 맞는 채널·역할 선택창 또는 문구 입력창을 엽니다. `/제거`는 제거 가능한 항목만, `/초기화`는 초기화 가능한 목록만 선택할 수 있습니다.", inline=False)
        embed.set_author(name="아래 메뉴에서 카테고리를 골라 자세히 보세요 👇")
        return embed

    if page == "common":
        embed.title = "🧰 공통 명령어"
        embed.description = "각 관리 명령어의 사용법입니다."
        embed.add_field(name="⚙️ /설정 [기능]", value="기능을 고르면 채널·역할 선택창 또는 문구 입력창이 열립니다.", inline=False)
        embed.add_field(name="📋 /체크 [기능]", value="현재 설정된 값을 확인합니다.", inline=False)
        embed.add_field(name="🗑️ /제거 [기능]", value="설정을 지우거나, 목록에서 하나만 골라 뺍니다.", inline=False)
        embed.add_field(name="♻️ /초기화 [기능]", value="목록을 전부 비웁니다. 경고 메시지의 ✅ 를 눌러야 실행되고 ❌ 는 취소예요.", inline=False)
        embed.add_field(name="⛔ /비활성화 [기능]", value="선택한 기능을 끕니다. 저장된 설정값은 유지됩니다.", inline=False)
        embed.add_field(name="✅ /모두활성화", value="전체 기능을 한 번에 활성화합니다.", inline=False)
        embed.add_field(name="⛔ /모두비활성화", value="전체 기능을 한 번에 비활성화합니다.", inline=False)
        return embed

    emoji, desc = CATEGORIES[page]
    embed.title = f"{emoji} {page} 카테고리"
    embed.description = desc
    for k, f in FEATURES.items():
        if f["cat"] != page:
            continue
        cmds = " · ".join(f"`{c}`" for c in available_cmds(k))
        usage = f" {f['usage']}" if f.get("usage") else ""
        embed.add_field(
            name=f"『{f['cat']}』 {k}",
            value=f"{f['desc']}\n└ {cmds}{usage}",
            inline=False
        )
    warn = CATEGORY_WARN.get(page)
    if warn:
        embed.add_field(name="🚨 주의", value=warn, inline=False)
    return embed

class GuideView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.interaction = None
        options = [discord.SelectOption(label="시작하기", value="home", emoji="🏠", description="명령어 사용법과 설정 방법")]
        for cat, (emoji, desc) in CATEGORIES.items():
            options.append(discord.SelectOption(label=cat, value=cat, emoji=emoji, description=desc[:100]))
        options.append(discord.SelectOption(label="공통 명령어", value="common", emoji="🧰", description="설정·체크·제거·초기화 사용법"))
        select = discord.ui.Select(placeholder="📚 보고 싶은 항목을 선택하세요", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        page = interaction.data["values"][0]
        await interaction.response.edit_message(embed=build_guide_embed(self.guild_id, page), view=self)

    async def on_timeout(self):
        if self.interaction:
            try:
                await self.interaction.edit_original_response(view=None)
            except discord.HTTPException:
                pass

@app_commands.guild_only()
async def guide_cb(interaction: discord.Interaction):
    view = GuideView(interaction.guild.id)
    await interaction.response.send_message(embed=build_guide_embed(interaction.guild.id, "home"), view=view, ephemeral=True)
    view.interaction = interaction


# ---------- /번역 (모든 사용자 사용 가능, 활성화 후 표시) ----------
@app_commands.choices(언어=[
    app_commands.Choice(name="English", value="en"),
    app_commands.Choice(name="日本語", value="ja"),
    app_commands.Choice(name="한국어", value="ko"),
])
@app_commands.describe(언어="번역 결과 언어를 선택하세요.", 내용="번역할 문장이나 약자/신조어를 입력하세요.")
@app_commands.guild_only()
async def translate_cb(interaction: discord.Interaction, 언어: app_commands.Choice[str], 내용: str):
    text_to_translate = 내용.strip()
    if not text_to_translate:
        await interaction.response.send_message("번역할 문장이나 약자를 입력해 주세요.", ephemeral=True)
        return
    if len(text_to_translate) > 1500:
        await interaction.response.send_message("한 번에 최대 1,500자까지 번역할 수 있습니다.", ephemeral=True)
        return

    # 자주 쓰는 영어 채팅/게임 약자를 뜻이 분명한 영어 표현으로 먼저 풀어 씁니다.
    # 예: ty -> thank you, gg -> good game, ez -> easy
    slang_meanings = {
        "ty": "thank you", "thx": "thanks", "tx": "thanks",
        "gg": "good game", "ggs": "good games", "ggwp": "good game, well played",
        "wp": "well played", "ez": "easy", "ezz": "easy",
        "np": "no problem", "nvm": "never mind", "yw": "you're welcome",
        "pls": "please", "plz": "please", "ppl": "people",
        "brb": "be right back", "afk": "away from keyboard",
        "idk": "I don't know", "ikr": "I know, right", "imo": "in my opinion",
        "imho": "in my humble opinion", "tbh": "to be honest", "ngl": "not gonna lie",
        "btw": "by the way", "fyi": "for your information", "asap": "as soon as possible",
        "omg": "oh my god", "lol": "laughing out loud", "lmao": "that's hilarious",
        "rofl": "rolling on the floor laughing", "smh": "shaking my head",
        "rn": "right now", "fr": "for real", "irl": "in real life",
        "wyd": "what are you doing", "wdym": "what do you mean", "wym": "what do you mean",
        "hbu": "how about you", "wbu": "what about you", "jk": "just kidding",
        "gl": "good luck", "hf": "have fun", "glhf": "good luck, have fun",
        "omw": "on my way", "ttyl": "talk to you later", "cya": "see you",
        "bc": "because", "b/c": "because", "u": "you", "ur": "your",
        "r": "are", "thx": "thanks", "k": "okay", "kk": "okay",
    }

    def expand_slang(value: str) -> str:
        # 단어 경계를 기준으로 치환해 다른 단어 안의 글자는 바꾸지 않습니다.
        pattern = re.compile(r"(?<!\w)([A-Za-z]+(?:/[A-Za-z]+)?)(?!\w)")
        return pattern.sub(lambda match: slang_meanings.get(match.group(1).lower(), match.group(1)), value)

    expanded_text = expand_slang(text_to_translate)
    language_names = {"en": "English", "ja": "日本語", "ko": "한국어"}
    await interaction.response.defer(thinking=True)
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "auto",
            "tl": 언어.value,
            "dt": "t",
            "q": expanded_text,
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        translated = "".join(
            part[0] for part in (data[0] or [])
            if isinstance(part, list) and part and isinstance(part[0], str)
        ).strip()
        if not translated:
            raise ValueError("번역 결과가 비어 있습니다.")
        embed = discord.Embed(
            title=f"🌐 번역 결과 · {language_names[언어.value]}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="원문", value=text_to_translate[:1024], inline=False)
        if expanded_text.casefold() != text_to_translate.casefold():
            embed.add_field(name="약자 풀이", value=expanded_text[:1024], inline=False)
        embed.description = translated[:4000]
        await interaction.followup.send(embed=embed)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, IndexError, TypeError) as exc:
        await interaction.followup.send(
            f"❌ 번역에 실패했습니다. 잠시 후 다시 시도해 주세요. (`{type(exc).__name__}`)",
            ephemeral=True,
        )

# ====================================================================
# 📚 기능 카탈로그 (카테고리 · 명령어 매핑)
# ====================================================================
CATEGORIES = {
    "인증": ("🪪", "신입 유저 인증과 역할 지급"),
    "제재": ("🚨", "제재·검열·추방 관리"),
    "입퇴장": ("🚪", "유저 입장·퇴장 로그"),
    "청소": ("🧹", "메세지·스레드 청소"),
    "기타": ("📖", "사용법 안내"),
}

FEATURES = {
    # ---------- 인증 ----------
    "인증방": {
        "cat": "인증", "label": "인증 채널", "store": "auth_channel",
        "desc": "신입이 들어오면 인증 안내가 올라올 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("auth_channel", "인증 채널"),
        "remove": make_remove_channel("auth_channel", "인증 채널"),
        "check": make_check_channel("auth_channel", "인증 채널"),
    },
    "인증지급": {
        "cat": "인증", "label": "인증지급 역할", "store": "auth_role_ids",
        "desc": "인증 완료 시 지급할 기본 역할을 등록합니다. (여러 개 가능)", "usage": "`역할`",
        "descs": {"set": "인증 완료 시 지급할 기본 역할을 추가 등록합니다.", "remove": "인증지급 역할 중 선택한 하나만 제외합니다."},
        "set": set_auth_role_cb, "remove": remove_auth_role_cb, "check": check_auth_role_cb,
        "reset": make_reset("auth_role_ids", "인증지급 역할"),
    },
    "인증메세지": {
        "cat": "인증", "label": "인증 안내 메세지", "store": "auth_form",
        "desc": "신입 입장 시 인증방에 보낼 안내 문구를 등록합니다.", "usage": "`메세지`",
        "set": set_auth_form_cb, "check": check_auth_form_cb,
    },
    "인증완료": {
        "cat": "인증", "desc": "대기자에게 기본 역할과 추가 역할을 지급해 인증을 승인합니다.", "usage": "`유저` `추가역할`",
        "top": approve_user_cb,
    },
    # ---------- 제재 ----------
    "제재로그": {
        "cat": "제재", "label": "제재 로그 채널", "store": "punish_log_channel",
        "desc": "제재·전과·추방 내역이 기록될 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("punish_log_channel", "제재 로그 채널"),
        "remove": make_remove_channel("punish_log_channel", "제재 로그 채널"),
        "check": make_check_channel("punish_log_channel", "제재 로그 채널"),
    },
    "제재단어": {
        "cat": "제재", "label": "제재 단어", "store": "ban_words",
        "desc": "포함되면 메세지 삭제 + 자동 제재할 단어를 등록합니다.", "usage": "`단어`",
        "descs": {"set": "자동 제재할 단어를 등록합니다. 쉼표(,)로 여러 개 가능.", "remove": "등록된 제재 단어 중 선택한 하나만 삭제합니다."},
        "set": make_set_words("ban_words", "제재 단어", "🚨 **자동 제재 단어 추가 완료:**"),
        "remove": make_remove_word("ban_words", "제재 단어"),
        "check": make_check_words("ban_words", "제재 단어", "🚨"),
        "reset": make_reset("ban_words", "제재 단어"),
    },
    "검열단어": {
        "cat": "제재", "label": "검열 단어", "store": "censor_words",
        "desc": "포함되면 메세지를 스포일러로 바꿀 단어를 등록합니다.", "usage": "`단어`",
        "descs": {"set": "스포일러 처리할 단어를 등록합니다. 쉼표(,)로 여러 개 가능.", "remove": "등록된 검열 단어 중 선택한 하나만 삭제합니다."},
        "set": make_set_words("censor_words", "검열 단어", "🔒 **스포일러 검열 단어 추가 완료:**"),
        "remove": make_remove_word("censor_words", "검열 단어"),
        "check": make_check_words("censor_words", "검열 단어", "🔒"),
        "reset": make_reset("censor_words", "검열 단어"),
    },
    "제재": {
        "cat": "제재", "desc": "멤버를 수동 제재합니다. (타임아웃 + 전과 역할 부여)", "usage": "`멤버` `사유`",
        "top": manual_punish_cb,
    },
    "제재지우기": {
        "cat": "제재", "desc": "타임아웃을 풀고 전과 단계를 1범 감면합니다.", "usage": "`멤버`",
        "top": clear_punish_cb,
    },
    "추방": {
        "cat": "제재", "desc": "멤버를 서버에서 추방하고 영구 차단합니다. (다시 못 들어옴)", "usage": "`멤버` `사유`",
        "top": ban_member_cb,
    },
    "추방해제": {
        "cat": "제재", "desc": "차단된 유저 목록을 보여주고 선택한 유저의 차단을 풉니다.", "usage": "(입력 후 목록에서 선택)",
        "top": unban_member_cb,
    },
    # ---------- 입퇴장 ----------
    "입장로그": {
        "cat": "입퇴장", "label": "입장 로그 채널", "store": "join_log_channel",
        "desc": "유저가 서버에 들어왔을 때 기록을 남길 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("join_log_channel", "입장 로그 채널"),
        "remove": make_remove_channel("join_log_channel", "입장 로그 채널"),
        "check": make_check_channel("join_log_channel", "입장 로그 채널"),
    },
    "퇴장로그": {
        "cat": "입퇴장", "label": "퇴장 로그 채널", "store": "leave_log_channel",
        "desc": "유저가 서버를 나갔을 때 기록을 남길 채널을 정합니다.", "usage": "`채널`",
        "set": make_set_channel("leave_log_channel", "퇴장 로그 채널"),
        "remove": make_remove_channel("leave_log_channel", "퇴장 로그 채널"),
        "check": make_check_channel("leave_log_channel", "퇴장 로그 채널"),
    },
    # ---------- 청소 ----------
    "청소": {
        "cat": "청소", "desc": "현재 채널을 같은 설정으로 재생성하고 고정 메시지/포스트를 복원합니다. (관리자, 확인 필요)", "usage": "",
        "top": clean_channel_cb,
    },
    "서버청소": {
        "cat": "청소", "desc": "채널·카테고리와 낫또봇 설정은 유지하고 메시지·스레드·포럼 게시글만 삭제합니다. (관리자, 확인 필요)", "usage": "",
        "top": clean_server_cb,
    },
    # ---------- 기타 ----------
    "번역": {
        "cat": "기타", "desc": "문장과 ty·gg·ez 같은 약자/게임 신조어도 뜻을 풀어 영어·일본어·한국어로 번역합니다. (모든 사용자 사용 가능)", "usage": "`언어` `내용`",
        "top": translate_cb,
    },
    "가이드": {
        "cat": "기타", "desc": "낫또봇 사용법을 카테고리별로 보여줍니다.", "usage": "",
        "top": guide_cb,
    },
}

VERB_INFO = {
    "설정": ("set", "활성화한 기능의 값을 설정합니다. (관리자 전용)"),
    "제거": ("remove", "활성화한 기능의 설정을 제거합니다. (관리자 전용)"),
    "체크": ("check", "활성화한 기능의 현재 설정을 확인합니다. (관리자 전용)"),
    "초기화": ("reset", "활성화한 기능의 목록을 모두 비웁니다. (관리자 전용)"),
}

def available_cmds(key: str) -> list:
    f = FEATURES[key]
    if "top" in f:
        return [f"/{key}"]
    out = []
    if "set" in f:
        out.append(f"/설정 {key}")
    if "check" in f:
        out.append(f"/체크 {key}")
    if "remove" in f:
        out.append(f"/제거 {key}")
    if "reset" in f:
        out.append(f"/초기화 {key}")
    return out

def leaf_description(verb: str, field: str, f: dict) -> str:
    custom = f.get("descs", {}).get(field)
    if custom:
        return custom[:100]
    label = f["label"]
    return {
        "설정": f"{label}{josa(label, '을', '를')} 설정합니다.",
        "제거": f"{label} 설정을 제거합니다.",
        "체크": f"현재 {label} 설정을 확인합니다.",
        "초기화": f"등록된 {label}{josa(label, '을', '를')} 모두 초기화합니다. (✅/❌ 확인)",
    }[verb][:100]

# ====================================================================
# 🧩 활성화 시스템 (서버별 명령어 동적 등록)
# ====================================================================
_GUILD_LOCKS = {}

def get_lock(guild_id: int) -> asyncio.Lock:
    return _GUILD_LOCKS.setdefault(guild_id, asyncio.Lock())


def feature_has_saved_value(guild_id: int, key: str) -> bool:
    """현재 서버에 해당 기능의 설정값/목록이 실제로 등록되어 있는지 확인합니다."""
    feature = FEATURES.get(key, {})
    gs = GUILD_SETTINGS.get(guild_id, {})
    if key == "인증지급":
        return bool(gs.get("auth_role_ids", []))
    if key == "제재단어":
        return bool(gs.get("ban_words", []))
    if key == "검열단어":
        return bool(gs.get("censor_words", []))
    if key == "인증메세지":
        return bool(str(gs.get("auth_form", "")).strip())
    store = feature.get("store")
    if store:
        value = gs.get(store)
        return value is not None and value != "" and value != 0 and value != []
    return False


def category_choice_label(key: str) -> str:
    return f"『{FEATURES[key]['cat']}』 {key}"[:100]

async def feature_autocomplete_for(interaction: discord.Interaction, current: str, verb: str):
    enabled = enabled_keys(interaction.guild.id) if interaction.guild else set()
    cur = current.lower()
    choices = []
    for key, feature in FEATURES.items():
        if key not in enabled or verb not in feature or cur not in key.lower():
            continue
        configured = feature_has_saved_value(interaction.guild.id, key) if interaction.guild else False
        # 제재단어/검열단어는 누적 등록 기능이므로 이미 단어가 있어도 설정 목록에 계속 표시합니다.
        # 그 외 설정 항목은 값이 있으면 중복 설정을 막기 위해 설정 목록에서 숨깁니다.
        if verb == "set" and configured and key not in ("제재단어", "검열단어"):
            continue
        if verb in ("remove", "reset") and not configured:
            continue
        choices.append(app_commands.Choice(name=category_choice_label(key), value=key))
    return choices[:25]

async def set_feature_autocomplete(interaction: discord.Interaction, current: str):
    return await feature_autocomplete_for(interaction, current, "set")

async def remove_feature_autocomplete(interaction: discord.Interaction, current: str):
    return await feature_autocomplete_for(interaction, current, "remove")

async def reset_feature_autocomplete(interaction: discord.Interaction, current: str):
    return await feature_autocomplete_for(interaction, current, "reset")

async def feature_autocomplete(interaction: discord.Interaction, current: str):
    return await feature_autocomplete_for(interaction, current, "check")

async def all_feature_autocomplete(interaction: discord.Interaction, current: str):
    cur = current.lower()
    enabled = enabled_keys(interaction.guild.id) if interaction.guild else set()
    return [app_commands.Choice(name=category_choice_label(key), value=key)
            for key in FEATURES if key not in enabled and cur in key.lower()][:25]

class SettingChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, feature_key: str):
        super().__init__(placeholder="설정할 채널을 선택하세요", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.feature_key = feature_key

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        feature = FEATURES[self.feature_key]
        get_gs(interaction.guild.id)[feature["store"]] = channel.id
        save_settings(GUILD_SETTINGS)
        await interaction.response.edit_message(content=f"✅ **{self.feature_key}**을(를) {channel.mention}(으)로 설정했습니다.", view=None)

class SettingRoleSelect(discord.ui.RoleSelect):
    def __init__(self, feature_key: str):
        super().__init__(placeholder="지급할 역할을 선택하세요", min_values=1, max_values=1)
        self.feature_key = feature_key

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        if role >= interaction.guild.me.top_role:
            await interaction.response.send_message("❌ 봇의 서열과 같거나 더 높은 역할은 자동 지급용으로 등록할 수 없습니다.", ephemeral=True)
            return
        role_ids = get_gs(interaction.guild.id).setdefault("auth_role_ids", [])
        if role.id in role_ids:
            await interaction.response.edit_message(content=f"⚠️ **{safe_text(role.name)}** 역할은 이미 등록되어 있습니다.", view=None)
            return
        role_ids.append(role.id)
        save_settings(GUILD_SETTINGS)
        await interaction.response.edit_message(content=f"✅ 인증 지급 역할에 **{safe_text(role.name)}** 역할을 추가했습니다.", view=None)

class SettingTextModal(discord.ui.Modal):
    def __init__(self, feature_key: str):
        self.feature_key = feature_key
        if feature_key == "인증메세지":
            title = "인증 안내 문구 설정"
            label = "인증방에 표시할 문구"
            placeholder = "줄바꿈은 그대로 입력할 수 있습니다."
            max_length = 4000
        else:
            title = f"{feature_key} 설정"
            label = "단어 입력 (여러 개는 쉼표로 구분)"
            placeholder = "예: 단어1, 단어2, 단어3"
            max_length = 1000
        super().__init__(title=title, timeout=300)
        self.value_input = discord.ui.TextInput(label=label, placeholder=placeholder, style=discord.TextStyle.paragraph, required=True, max_length=max_length)
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = str(self.value_input.value).strip()
        if not value:
            await interaction.response.send_message("❌ 내용을 입력해 주세요.", ephemeral=True)
            return
        gs = get_gs(interaction.guild.id)
        if self.feature_key == "인증메세지":
            gs["auth_form"] = value
            result = "✅ 인증 안내 메세지를 저장했습니다."
        else:
            key = "ban_words" if self.feature_key == "제재단어" else "censor_words"
            words = gs.setdefault(key, [])
            added = [word.strip() for word in value.split(",") if word.strip()]
            if not added:
                await interaction.response.send_message("⚠️ 등록할 단어를 입력해 주세요.", ephemeral=True)
                return
            # 기존 목록에 같은 단어가 있어도 중복 등록합니다.
            words.extend(added)
            result = f"✅ {self.feature_key} 등록 완료: {', '.join(added)}"
        save_settings(GUILD_SETTINGS)
        await interaction.response.send_message(result, ephemeral=True)

@app_commands.describe(기능="설정할 기능을 선택하세요.")
@app_commands.autocomplete(기능=set_feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_set_cb(interaction: discord.Interaction, 기능: str):
    feature = FEATURES.get(기능)
    if not feature or "set" not in feature:
        await interaction.response.send_message("❌ 설정할 수 있는 기능을 목록에서 선택해 주세요.", ephemeral=True)
        return
    if 기능 not in enabled_keys(interaction.guild.id):
        await interaction.response.send_message(f"❌ 먼저 `/활성화`에서 **{기능}** 기능을 활성화해 주세요.", ephemeral=True)
        return
    if 기능 in ("인증방", "제재로그", "입장로그", "퇴장로그"):
        view = discord.ui.View(timeout=180)
        view.add_item(SettingChannelSelect(기능))
        await interaction.response.send_message(f"**{기능}** 설정: 아래에서 채널을 선택하세요.", view=view, ephemeral=True)
    elif 기능 == "인증지급":
        view = discord.ui.View(timeout=180)
        view.add_item(SettingRoleSelect(기능))
        await interaction.response.send_message("**인증지급** 설정: 아래에서 지급할 역할을 선택하세요.", view=view, ephemeral=True)
    elif 기능 in ("인증메세지", "제재단어", "검열단어"):
        await interaction.response.send_modal(SettingTextModal(기능))
    else:
        await interaction.response.send_message("❌ 이 항목은 설정 UI를 지원하지 않습니다.", ephemeral=True)

@app_commands.describe(기능="확인할 기능을 선택하세요.")
@app_commands.autocomplete(기능=feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_check_cb(interaction: discord.Interaction, 기능: str):
    f = FEATURES.get(기능, {}); gs = GUILD_SETTINGS.get(interaction.guild.id, {})
    store = f.get("store")
    if 기능 in ("인증방", "제재로그", "입장로그", "퇴장로그"):
        ch = interaction.guild.get_channel(gs.get(store, 0)); value = ch.mention if ch else "설정되지 않음"
    elif 기능 == "인증지급":
        value = ", ".join(r.mention for r in (interaction.guild.get_role(x) for x in gs.get("auth_role_ids", [])) if r) or "설정되지 않음"
    elif 기능 == "인증메세지": value = gs.get("auth_form", "설정되지 않음").replace("\\\\n", "\\n")
    elif 기능 in ("제재단어", "검열단어"):
        value = ", ".join(gs.get("ban_words" if 기능 == "제재단어" else "censor_words", [])) or "등록된 단어 없음"
    else: value = "이 기능은 별도 설정값이 없습니다."
    await interaction.response.send_message(f"📋 **{기능} 현재 설정**\\n{value[:1800]}", ephemeral=True)

@app_commands.describe(기능="제거할 설정 항목을 선택하세요.", 값="단어 목록에서 특정 단어를 제거할 때 입력하세요. 비우면 목록 전체를 비웁니다.")
@app_commands.autocomplete(기능=remove_feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_remove_cb(interaction: discord.Interaction, 기능: str, 값: str = ""):
    f = FEATURES.get(기능, {})
    if "remove" not in f:
        await interaction.response.send_message("❌ 제거할 수 있는 항목을 목록에서 선택해 주세요.", ephemeral=True); return
    gs = GUILD_SETTINGS.get(interaction.guild.id, {}); store = f.get("store")
    if 기능 in ("제재단어", "검열단어"):
        key = "ban_words" if 기능 == "제재단어" else "censor_words"; words = gs.get(key, [])
        if 값:
            if 값 not in words:
                await interaction.response.send_message("❌ 해당 단어가 등록되어 있지 않습니다.", ephemeral=True); return
            words.remove(값); result = f"🗑️ `{값}` 단어를 제거했습니다."
        else: gs[key] = []; result = f"🗑️ {기능} 목록을 비웠습니다."
    elif 기능 == "인증지급": gs["auth_role_ids"] = []; result = "🗑️ 인증지급 역할 목록을 비웠습니다."
    elif store and store in gs: del gs[store]; result = f"🗑️ {기능} 설정을 제거했습니다."
    else:
        await interaction.response.send_message("❌ 제거할 설정이 없습니다.", ephemeral=True); return
    save_settings(GUILD_SETTINGS); await interaction.response.send_message(result, ephemeral=True)

@app_commands.describe(기능="초기화할 목록 기능을 선택하세요.")
@app_commands.autocomplete(기능=reset_feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_reset_cb(interaction: discord.Interaction, 기능: str):
    if 기능 not in FEATURES or "reset" not in FEATURES[기능]:
        await interaction.response.send_message("❌ 초기화할 수 있는 목록을 선택해 주세요.", ephemeral=True); return
    gs = GUILD_SETTINGS.get(interaction.guild.id, {})
    key = {"인증지급":"auth_role_ids", "제재단어":"ban_words", "검열단어":"censor_words"}.get(기능)
    if not key:
        await interaction.response.send_message("❌ 이 기능은 목록 초기화를 지원하지 않습니다. `/제거`를 이용해 주세요.", ephemeral=True); return
    old = gs.get(key, [])
    if not old:
        await interaction.response.send_message("⚠️ 초기화할 항목이 없습니다.", ephemeral=True); return
    gs[key] = []; save_settings(GUILD_SETTINGS)
    await interaction.response.send_message(f"♻️ **{기능}** 목록의 {len(old)}개 항목을 초기화했습니다.", ephemeral=True)

@app_commands.describe(기능="활성화할 기능을 선택하세요.")
@app_commands.autocomplete(기능=all_feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_activate_cb(interaction: discord.Interaction, 기능: str):
    if 기능 not in FEATURES:
        await interaction.response.send_message("❌ 목록에서 기능을 선택해 주세요.", ephemeral=True); return
    await activate_features(interaction, [기능])

@app_commands.describe(대상="역할을 지급할 멤버", 역할="지급할 역할")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_roles=True)
async def give_role_cb(interaction: discord.Interaction, 대상: discord.Member, 역할: discord.Role):
    if 역할 >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ 봇보다 높거나 같은 역할은 지급할 수 없습니다.", ephemeral=True); return
    try:
        await 대상.add_roles(역할, reason=f"/역할주기 실행자: {interaction.user}")
        await interaction.response.send_message(f"✅ {대상.mention}님에게 {역할.mention} 역할을 지급했습니다.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ 봇의 역할 순위 또는 권한이 부족합니다.", ephemeral=True)

def _with_default_permissions(command: app_commands.Command, permissions: discord.Permissions) -> app_commands.Command:
    """Apply Discord command default permissions using discord.py's decorator API."""
    permission_names = ("administrator", "manage_roles", "moderate_members", "ban_members")
    kwargs = {name: True for name in permission_names if getattr(permissions, name, False)}
    return app_commands.default_permissions(**kwargs)(command)

def build_guild_commands(guild_id: int) -> list:
    enabled = enabled_keys(guild_id)
    if not enabled:
        return []
    admin_permissions = discord.Permissions(administrator=True)
    result = [
        _with_default_permissions(app_commands.Command(name="설정", description="기능을 선택해 값을 설정합니다.", callback=generic_set_cb), admin_permissions),
        _with_default_permissions(app_commands.Command(name="체크", description="기능의 현재 설정을 확인합니다.", callback=generic_check_cb), admin_permissions),
        _with_default_permissions(app_commands.Command(name="제거", description="기능의 설정 또는 목록 항목을 제거합니다.", callback=generic_remove_cb), admin_permissions),
        _with_default_permissions(app_commands.Command(name="초기화", description="기능의 목록을 초기화합니다.", callback=generic_reset_cb), admin_permissions),
        _with_default_permissions(app_commands.Command(name="비활성화", description="켜 둔 기능을 끕니다.", callback=deactivate_cb), admin_permissions),
        _with_default_permissions(app_commands.Command(name="역할주기", description="선택한 멤버에게 역할을 지급합니다.", callback=give_role_cb), discord.Permissions(manage_roles=True)),
    ]
    command_permissions = {
        "인증완료": discord.Permissions(manage_roles=True),
        "제재": discord.Permissions(moderate_members=True),
        "제재지우기": discord.Permissions(moderate_members=True),
        "추방": discord.Permissions(ban_members=True),
        "추방해제": discord.Permissions(ban_members=True),
        "청소": admin_permissions,
        "서버청소": admin_permissions,
    }
    public_commands = {"번역", "가이드"}
    for k, f in FEATURES.items():
        if k in enabled and "top" in f:
            command = app_commands.Command(
                name=k, description=f["desc"][:100], callback=f["top"]
            )
            if k in command_permissions:
                command = _with_default_permissions(command, command_permissions[k])
            elif k not in public_commands:
                # 기능 관리/운영 명령어는 관리자 권한이 없는 계정의 목록에 노출하지 않습니다.
                command = _with_default_permissions(command, admin_permissions)
            result.append(command)
    return result

def register_guild_commands(guild_id: int) -> discord.Object:
    """로컬 명령어 트리만 갱신합니다. (디스코드 서버 동기화는 하지 않음)"""
    obj = discord.Object(id=guild_id)
    bot.tree.clear_commands(guild=obj)
    for c in build_guild_commands(guild_id):
        bot.tree.add_command(c, guild=obj, override=True)
    return obj

async def refresh_guild_commands(guild: discord.Guild):
    obj = register_guild_commands(guild.id)
    await bot.tree.sync(guild=obj)

def activation_embed(keys: list) -> discord.Embed:
    embed = discord.Embed(title="✅ 활성화 완료", description="아래 명령어를 이제 사용할 수 있어요!", color=discord.Color.green())
    for k in keys:
        f = FEATURES[k]
        cmds = " · ".join(f"`{c}`" for c in available_cmds(k))
        embed.add_field(name=f"{CATEGORIES[f['cat']][0]} {k}", value=f"{f['desc']}\n└ {cmds}", inline=False)
    embed.set_footer(text="명령어가 안 보이면 몇 초 기다렸다가 디스코드를 Ctrl+R 로 새로고침해 보세요.")
    return embed

async def activate_features(interaction: discord.Interaction, keys: list):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)
    async with get_lock(guild.id):
        gs = get_gs(guild.id)
        old = list(gs.get("enabled", []))
        new = [k for k in keys if k not in old]
        if not new:
            await interaction.followup.send("⚠️ 선택한 기능은 이미 모두 활성화되어 있습니다.")
            return
        gs["enabled"] = [k for k in FEATURES if k in old or k in new]
        save_settings(GUILD_SETTINGS)
        try:
            await refresh_guild_commands(guild)
        except discord.HTTPException as e:
            gs["enabled"] = old
            save_settings(GUILD_SETTINGS)
            register_guild_commands(guild.id)
            await interaction.followup.send(f"❌ 명령어 등록에 실패했습니다. 잠시 후 다시 시도해 주세요.\n`{e}`")
            return
    await interaction.followup.send(embed=activation_embed(new))

def make_activate_cb(keys: list):
    @admin_only
    async def cb(interaction: discord.Interaction):
        await activate_features(interaction, keys)
    return cb

async def deactivate_autocomplete(interaction: discord.Interaction, current: str):
    if not interaction.guild:
        return []
    cur = current.lower()
    choices = []
    for k in FEATURES:
        if k in enabled_keys(interaction.guild.id) and cur in k.lower():
            choices.append(app_commands.Choice(name=category_choice_label(k), value=k))
    return choices[:25]

@app_commands.describe(기능="비활성화할 기능을 목록에서 선택하세요.")
@app_commands.autocomplete(기능=deactivate_autocomplete)
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
@admin_only
async def deactivate_cb(interaction: discord.Interaction, 기능: str):
    guild = interaction.guild
    if 기능 not in enabled_keys(guild.id):
        await interaction.response.send_message("❌ 활성화되어 있지 않은 기능입니다. 목록에서 선택해 주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with get_lock(guild.id):
        gs = get_gs(guild.id)
        old = list(gs.get("enabled", []))
        gs["enabled"] = [k for k in old if k != 기능]
        save_settings(GUILD_SETTINGS)
        try:
            await refresh_guild_commands(guild)
        except discord.HTTPException as e:
            gs["enabled"] = old
            save_settings(GUILD_SETTINGS)
            register_guild_commands(guild.id)
            await interaction.followup.send(f"❌ 명령어 갱신에 실패했습니다. 잠시 후 다시 시도해 주세요.\n`{e}`")
            return
    await interaction.followup.send(f"⛔ **{기능}** 기능을 비활성화했습니다. 저장된 설정값은 남아 있으니 `/활성화`에서 『{FEATURES[기능]['cat']}』 {기능} 선택 로 다시 켜면 그대로 쓸 수 있어요.")

def build_activation_group() -> app_commands.Command:
    return _with_default_permissions(app_commands.Command(name="활성화", description="사용할 기능을 검색해 하나씩 활성화합니다.", callback=generic_activate_cb), discord.Permissions(administrator=True))

@app_commands.guild_only()
@admin_only
async def activate_all_cb(interaction: discord.Interaction):
    await activate_features(interaction, list(FEATURES.keys()))

def build_activate_all_command() -> app_commands.Command:
    return _with_default_permissions(app_commands.Command(name="모두활성화", description="모든 기능을 한 번에 활성화합니다.", callback=activate_all_cb), discord.Permissions(administrator=True))

@app_commands.guild_only()
@admin_only
async def deactivate_all_cb(interaction: discord.Interaction):
    guild = interaction.guild
    old = list(enabled_keys(guild.id))
    if not old:
        await interaction.response.send_message("⚠️ 비활성화할 기능이 없습니다.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with get_lock(guild.id):
        gs = get_gs(guild.id)
        previous = list(gs.get("enabled", []))
        gs["enabled"] = []
        save_settings(GUILD_SETTINGS)
        try:
            await refresh_guild_commands(guild)
        except discord.HTTPException as e:
            gs["enabled"] = previous
            save_settings(GUILD_SETTINGS)
            register_guild_commands(guild.id)
            await interaction.followup.send(f"❌ 전체 비활성화 중 명령어 갱신에 실패했습니다.\\n`{e}`")
            return
    await interaction.followup.send(f"⛔ 전체 기능 {len(old)}개를 비활성화했습니다. 저장된 설정값은 유지됩니다.")

def build_deactivate_all_command() -> app_commands.Command:
    return _with_default_permissions(app_commands.Command(name="모두비활성화", description="모든 기능을 한 번에 비활성화합니다.", callback=deactivate_all_cb), discord.Permissions(administrator=True))

# ================= [ 실시간 제재/검열 단어 처리 ] =================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot or not isinstance(message.author, discord.Member):
        return

    settings = GUILD_SETTINGS.get(message.guild.id, {})
    content = message.content or ""
    folded_content = content.casefold()

    # 제재단어가 검열단어보다 우선합니다. 등록된 단어는 대소문자를 구분하지 않습니다.
    ban_words = [str(word).strip() for word in settings.get("ban_words", []) if str(word).strip()]
    if is_on(message.guild.id, "제재단어"):
        matched = next((word for word in ban_words if word.casefold() in folded_content), None)
        if matched is not None:
            try:
                await message.delete()
            except discord.HTTPException as exc:
                print(f"제재단어 메시지 삭제 실패: {exc}")
            reason = f"자동 제재 단어 [{matched}] 포함 메시지 작성"
            await punish_user(
                message.guild,
                message.author,
                f"{reason}\n포착된 메시지: ||{content}||"[:1500],
                "시스템 자동 검열",
            )
            return

    censor_words = [str(word).strip() for word in settings.get("censor_words", []) if str(word).strip()]
    if is_on(message.guild.id, "검열단어"):
        matched = next((word for word in censor_words if word.casefold() in folded_content), None)
        if matched is not None:
            try:
                await message.delete()
            except discord.HTTPException as exc:
                print(f"검열단어 메시지 삭제 실패: {exc}")
            # 원문은 스포일러로 감싸 재게시해 내용을 클릭해야만 보이게 합니다.
            safe_content = content[:1900]
            try:
                await message.channel.send(
                    f"🔒 {message.author.mention}님의 메시지가 검열 단어를 포함해 스포일러 처리되었습니다.\n||{safe_content}||",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                print(f"검열 메시지 재게시 실패: {exc}")
            return

    await bot.process_commands(message)


# ================= [ 실행 ] =================
if __name__ == "__main__":
    import time
    keep_alive()
    time.sleep(10)  # 재시작 직후 연속 로그인으로 인한 레이트 리밋 방지
    try:
        bot.run(os.getenv("DISCORD_TOKEN"))
    except discord.HTTPException as e:
        print(f"⚠️ 로그인 실패 (status={e.status}). 10분 대기 후 종료합니다.")
        time.sleep(600)
        raise
