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
COMMAND_VERSION = 7   # 명령어 권한 정책 변경 버전
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
        # 삭제된 기능(예: 서버청소)이 저장된 활성화 목록에 남아 있으면 정리합니다.
        for _gs in GUILD_SETTINGS.values():
            if isinstance(_gs.get("enabled"), list):
                _gs["enabled"] = [k for k in _gs["enabled"] if k in FEATURES]
        save_settings(GUILD_SETTINGS)
        # 전역 명령어로 기능 활성화 및 전체 활성화/비활성화를 등록합니다.
        self.tree.add_command(build_activation_group())
        self.tree.add_command(build_activate_all_command())
        self.tree.add_command(build_deactivate_all_command())
        # 문의함 버튼은 봇이 재시작되어도 계속 눌릴 수 있도록 영구 등록합니다.
        self.add_view(InquiryOpenView())
        self.add_view(InquiryCloseView())
        self.add_view(ReportOpenView())
        self.add_view(ReportDecisionView())
        self.add_view(ReportCloseView())
        # 문의방 '닫기' 버튼이 대화에 묻히지 않도록 5분마다 다시 올려 줍니다.
        self._inquiry_reminder_task = asyncio.create_task(inquiry_reminder_loop())
        # 전역 명령어의 최신 관리자 권한 설정을 디스코드에 반영합니다.
        await self.tree.sync()
        # 서버별 명령어를 전부 재등록/동기화합니다. 비활성 서버도 동기화해야
        # 디스코드에 남아 있는 오래된 명령어가 계속 노출되는 일을 막을 수 있습니다.
        for gid, gs in list(GUILD_SETTINGS.items()):
            obj = register_guild_commands(gid)
            try:
                await self.tree.sync(guild=obj)
            except discord.HTTPException as exc:
                print(f"서버 {gid} 명령어 권한 동기화 실패: {exc}")

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
                f"❌ 확인 절차에서 오류가 발생해 **{action}**{josa(action, '을', '를')} 취소했습니다. 기존 상태는 변경하지 않았습니다.",
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
                f"🚫 **{action}**{josa(action, '이', '가')} 취소되었습니다. 기존 상태는 그대로 유지됩니다.", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return False

    try:
        await interaction.followup.send(
            f"⏰ {CONFIRM_TIMEOUT}초 동안 선택하지 않아 **{action}**{josa(action, '이', '가')} 자동으로 취소되었습니다. 기존 상태는 그대로 유지됩니다.",
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
# 🛠️ 개별 기능 명령어 (/인증완료 /제재 /제재지우기 /추방 /추방해제 /청소 /가이드)
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
        await interaction.followup.send("❌ 지급할 기본 역할이나 추가역할이 없습니다. `/설정 인증지급` 으로 먼저 등록해 주세요.")
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


# ---------- 청소 ----------
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
        await interaction.response.send_message("⏳ 이 서버에서 이미 청소가 진행 중입니다. 끝난 뒤 다시 시도해 주세요.", ephemeral=True)
        return
    if not isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)) and not (
        hasattr(discord, "ForumChannel") and isinstance(channel, discord.ForumChannel)
    ):
        await interaction.response.send_message("❌ 일반 채널·공지 채널·음성·스테이지·포럼 채널에서만 사용할 수 있습니다.", ephemeral=True)
        return
    warn_text = (
        f"🧹 **경고: 채널 청소**\n"
        f"{interaction.user.mention}님, {channel.mention} 채널을 같은 설정으로 새로 만들고 기존 채널을 삭제합니다.\n"
        f"채널 권한·설정·위치와 고정 메시지/고정 포스트는 그대로 복원되며, 일반 메시지는 복원되지 않습니다.\n"
        f"채널 ID는 바뀌지만, 낫또봇에 저장된 채널 설정은 새 ID로 자동 갱신됩니다.\n\n"
        f"✅ 진행 / ❌ 취소 (본인만 선택 가능, {CONFIRM_TIMEOUT}초 후 자동 취소)"
    )
    if not await confirm_by_reaction(interaction, warn_text, "청소"):
        return

    RUNNING_CLEANS.add(guild.id)
    try:
        new_channel = await _recreate_one_channel(interaction, channel)
        await interaction.followup.send(
            f"✅ **청소 완료:** {new_channel.mention}\n"
            f"채널 설정과 고정 메시지/포스트를 복원했습니다. 일반 메시지는 복원하지 않았습니다.",
            ephemeral=True,
        )
    except Exception as exc:
        await interaction.followup.send(
            f"❌ 청소에 실패했습니다. 기존 채널이 남아 있는지 확인해 주세요.\n`{str(exc)[:1200]}`",
            ephemeral=True,
        )
    finally:
        RUNNING_CLEANS.discard(guild.id)

# ---------- 가이드 ----------
# 개별 명령어를 실행하는 데 필요한 권한 (표에 없으면 관리자)
PERM_LABELS = {
    "인증완료": "역할 관리 권한",
    "제재": "멤버 타임아웃 권한",
    "제재지우기": "멤버 타임아웃 권한",
    "추방": "멤버 차단 권한",
    "추방해제": "멤버 차단 권한",
    "번역": "모든 멤버",
    "가이드": "모든 멤버",
}

def _feature_state(guild_id: int, key: str) -> tuple:
    """(아이콘, 상태 문구)를 돌려줍니다."""
    if key not in enabled_keys(guild_id):
        return "⚪", "꺼짐"
    if "top" in FEATURES[key]:
        return "🟢", "사용 중"
    if feature_has_saved_value(guild_id, key):
        return "🟢", "설정됨"
    return "🟡", "미설정"

def build_guide_embed(guild_id: int, page: str) -> discord.Embed:
    enabled = enabled_keys(guild_id)
    embed = discord.Embed(color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    embed.set_footer(text="안전하고 청정한 서버 환경을 구축합니다 • Powered by NattoBot")

    if page == "home":
        on_total = sum(1 for k in FEATURES if k in enabled)
        embed.title = "🛡️ 낫또봇 가이드"
        embed.description = (
            "인증 · 제재 · 로그 · 청소까지, 서버 운영에 필요한 도구를 한곳에서 관리합니다.\n"
            "아래 메뉴에서 카테고리를 골라 자세히 확인해 보세요."
        )
        embed.add_field(
            name="🚀 빠른 시작",
            value=(
                "**1.** `/활성화` 로 필요한 기능 켜기 (전부 켜려면 `/모두활성화`)\n"
                "**2.** `/설정` 으로 채널·역할·문구 등록\n"
                "**3.** `/체크` 로 설정이 잘 들어갔는지 확인"
            ),
            inline=False,
        )
        lines = []
        for cat, (emoji, desc) in CATEGORIES.items():
            keys = [k for k, f in FEATURES.items() if f["cat"] == cat]
            on = sum(1 for k in keys if k in enabled)
            lines.append(f"{emoji} **{cat}** — {desc} `{on}/{len(keys)}`")
        lines.append("⚠️ **주의사항** — 인증·제재·활성화·비활성화가 안 될 때")
        embed.add_field(name="📚 카테고리  (켜짐/전체)", value="\n".join(lines), inline=False)
        embed.add_field(
            name="🧰 공통 명령어",
            value="`/활성화` `/설정` `/체크` `/제거` `/초기화` `/비활성화` `/역할주기` 의 사용법은 **공통 명령어** 메뉴에서 볼 수 있어요.",
            inline=False,
        )
        embed.add_field(
            name="📊 우리 서버 현황",
            value=f"켜진 기능 **{on_total}** / {len(FEATURES)}개\n🟢 사용 중 · 🟡 켜졌지만 값 미설정 · ⚪ 꺼짐",
            inline=False,
        )
        return embed

    if page == "common":
        embed.title = "🧰 공통 명령어"
        embed.description = "기능을 켜고, 값을 설정하고, 관리하는 명령어입니다. (기본적으로 관리자 전용)"
        embed.add_field(
            name="🔌 기능 켜고 끄기",
            value=(
                "`/활성화 [기능]` — 선택한 기능을 켭니다.\n"
                "`/모두활성화` — 모든 기능을 한 번에 켭니다.\n"
                "`/비활성화 [기능]` — 선택한 기능을 끕니다. 저장된 설정값은 유지돼요.\n"
                "`/모두비활성화` — 모든 기능을 한 번에 끕니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ 설정 관리",
            value=(
                "`/설정 [기능]` — 채널·역할 선택창 또는 문구 입력창을 엽니다.\n"
                "`/체크 [기능]` — 현재 저장된 설정을 확인합니다.\n"
                "`/제거 [기능] [값]` — 설정을 지웁니다. 단어 기능은 `값`에 단어를 적으면 그 단어만, 비우면 전체를 지우고, `인증지급`은 등록된 역할 전체를 비웁니다.\n"
                "`/초기화 [기능]` — `인증지급`·`제재단어`·`검열단어` 목록을 모두 비웁니다. 확인창에서 ✅ 를 눌러야 실행되고 ❌ 는 취소예요."
            ),
            inline=False,
        )
        embed.add_field(
            name="🎖️ 역할 지급",
            value="`/역할주기 [대상] [역할]` — 선택한 멤버에게 역할을 지급합니다. (역할 관리 권한 · 봇보다 높은 역할은 불가)",
            inline=False,
        )
        return embed

    if page == "caution":
        embed.title = "⚠️ 주의사항"
        embed.description = "기능이 말을 듣지 않을 때, 어떤 문제가 생기는지와 해결 방법을 정리했어요."
        embed.color = discord.Color.orange()
        embed.add_field(
            name="🪪 인증이 안 될 때",
            value=(
                "**문제** `인증방`·`인증지급`·`인증메세지` 중 하나라도 꺼져 있거나 비어 있으면 신입에게 안내가 올라오지 않거나 역할이 지급되지 않아 가입 단계가 멈춰요. `인증방`이 없으면 `/인증완료`도 실행되지 않아요.\n"
                "**해결** ① `/활성화`에서 세 기능을 켜세요. ② `/설정 인증방` `/설정 인증지급` `/설정 인증메세지`를 채우세요. ③ `/체크`로 잘 들어갔는지 확인하세요. ④ 봇 역할을 지급할 역할보다 위에 두고, 인증방에 메시지를 보낼 권한을 주세요."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔨 제재가 안 될 때",
            value=(
                "**문제** `제재` 기능이 꺼져 있으면 `/제재` `/제재지우기` 명령어가 생기지 않고, `제재로그`가 비어 있으면 실행되지 않아요. 신고함의 **동의**를 눌러도 제재가 걸리지 않고요. 봇보다 역할이 높거나 같은 사람, 서버장, 봇은 제재할 수 없어요.\n"
                "**해결** ① `/활성화`에서 제재 기능을 켜세요. ② `/설정 제재로그`로 로그 채널을 정하세요. ③ 봇에게 멤버 타임아웃·차단 권한을 주고, 봇 역할을 제재할 사람들보다 위로 올리세요."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔌 활성화가 안 될 때",
            value=(
                "**문제** 활성화는 디스코드에 명령어를 등록하는 일이라, 등록에 실패하면 \"명령어 등록에 실패했습니다\" 안내가 뜨고 방금 켠 기능은 자동으로 취소돼요. 명령어가 목록에 안 보일 수도 있어요. 활성화는 서버 관리자만 할 수 있어요.\n"
                "**해결** ① 1~2분 뒤 `/활성화`를 다시 실행하세요. ② 디스코드를 Ctrl+R로 새로고침하세요. ③ 계속 안 되면 봇을 `applications.commands` 범위가 포함된 초대 링크로 다시 초대하세요."
            ),
            inline=False,
        )
        embed.add_field(
            name="⛔ 비활성화가 안 될 때",
            value=(
                "**문제** 같은 이유로 갱신에 실패하면 \"명령어 갱신에 실패했습니다\" 안내가 뜨고 기능은 그대로 켜져 있어요. 꺼도 저장된 설정값은 남아 있고, 문의함·신고함 버튼은 눌러도 비활성화 안내만 나와요. 이미 올린 안내글은 저절로 지워지지 않아요.\n"
                "**해결** ① 잠시 뒤 `/비활성화`를 다시 실행하세요. ② 한꺼번에 끄려면 `/모두비활성화`를 쓰세요. ③ 안내글까지 지우려면 켜 둔 상태에서 `/제거 문의함` `/제거 신고함`을 먼저 실행한 뒤 끄세요."
            ),
            inline=False,
        )
        return embed

    emoji, desc = CATEGORIES[page]
    embed.title = f"{emoji} {page}"
    embed.description = f"{desc}\n🟢 사용 중 · 🟡 켜졌지만 값 미설정 · ⚪ 꺼짐"
    for k, f in FEATURES.items():
        if f["cat"] != page:
            continue
        icon, state = _feature_state(guild_id, k)
        is_top = "top" in f
        title = f"/{k}" if is_top else k
        cmds = " · ".join(f"`{c}`" for c in available_cmds(k))
        usage = f" {f['usage']}" if f.get("usage") else ""
        lines = [f["desc"]]
        if is_top:
            lines.append(f"**사용** {cmds}{usage}")
        else:
            lines.append(f"**명령어** {cmds}")
            if f.get("usage"):
                lines.append(f"**입력값** {f['usage']}")
        lines.append(f"**권한** {PERM_LABELS.get(k, '관리자')}")
        if k not in enabled:
            lines.append("➜ `/활성화` 에서 먼저 켜 주세요.")
        embed.add_field(
            name=f"{icon} {title} · {state}",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    return embed

class GuideView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.interaction = None
        options = [discord.SelectOption(label="시작하기", value="home", emoji="🏠", description="빠른 시작과 전체 구성")]
        for cat, (emoji, desc) in CATEGORIES.items():
            options.append(discord.SelectOption(label=cat, value=cat, emoji=emoji, description=desc[:100]))
        options.append(discord.SelectOption(label="공통 명령어", value="common", emoji="🧰", description="활성화·설정·체크·제거·초기화·역할주기"))
        options.append(discord.SelectOption(label="주의사항", value="caution", emoji="⚠️", description="인증·제재·활성화·비활성화가 안 될 때"))
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
# 📨 문의함 (버튼으로 문의자 + 관리자 + 지정 역할만 보는 비공개 문의방 열기)
# ====================================================================
INQUIRY_FEATURE = "문의함"
INQUIRY_PANEL_TITLE = "📨 문의함"   # 안내글 제목은 고정이며 설정에서 바꿀 수 없습니다.
INQUIRY_DISABLED_TEXT = "⛔ 지금 이 기능은 관리자에 의해 비활성화 된 상황입니다."
INQUIRY_WAIT_TEXT = "관리자가 오실때까지 시간의 여유를 즐기세요...☕☕"
INQUIRY_CLOSE_DELAY = 5
_INQUIRY_LOCKS = {}
_CLOSING_INQUIRIES = set()
_LAST_REMINDER = {}                       # 채널 ID -> 마지막으로 올린 '닫기' 알림 메시지 ID
INQUIRY_REMINDER_INTERVAL = 300           # 5분
INQUIRY_REMINDER_TITLE = "🔒 문의가 끝나셨나요?"

def _inquiry_lock(guild_id: int) -> asyncio.Lock:
    return _INQUIRY_LOCKS.setdefault(guild_id, asyncio.Lock())

def _inquiry_owner_id(channel) -> int:
    """문의방 주제(topic)에 적어 둔 문의자 ID를 읽습니다. 문의방이 아니면 0."""
    match = re.search(r"inquiry:(\d+)", getattr(channel, "topic", None) or "")
    return int(match.group(1)) if match else 0

def _grantable(perms: discord.Permissions, *names: str) -> discord.PermissionOverwrite:
    """봇이 실제로 가진 권한만 골라 허용합니다. (없는 권한을 부여하려 하면 디스코드가 거부함)"""
    return discord.PermissionOverwrite(**{n: True for n in names if getattr(perms, n, False)})

_VIEWER_PERMS = ("view_channel", "send_messages", "read_message_history", "attach_files", "embed_links")

def _can_close_inquiry(interaction: discord.Interaction, channel, owner_id: int) -> bool:
    """문의를 연 일반 유저만 닫을 수 없습니다. 관리자·열람 역할·그 외 이 방을 볼 수 있는 사람은 닫을 수 있습니다."""
    user = interaction.user
    if interaction.permissions.administrator:
        return True
    if user.id != owner_id:
        return True
    # 문의자 본인이라도 열람 역할(담당자)을 가지고 있으면 닫을 수 있습니다.
    return any(
        channel.overwrites_for(role).view_channel
        for role in getattr(user, "roles", [])
        if not role.is_default()
    )

async def _delete_inquiry_panel(guild: discord.Guild, gs: dict):
    """저장된 문의함 안내글을 지웁니다. 이미 없거나 지울 수 없으면 조용히 넘어갑니다."""
    channel = guild.get_channel(gs.get("inquiry_channel", 0))
    message_id = gs.get("inquiry_panel_message")
    if channel and message_id:
        try:
            await (await channel.fetch_message(message_id)).delete()
        except discord.HTTPException:
            pass

async def open_inquiry_channel(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ 서버 안에서만 사용할 수 있습니다.", ephemeral=True)
        return
    # 관리자가 /비활성화 로 꺼 둔 경우
    if not is_on(guild.id, INQUIRY_FEATURE):
        await interaction.response.send_message(INQUIRY_DISABLED_TEXT, ephemeral=True)
        return

    member = interaction.user
    await interaction.response.defer(ephemeral=True)
    me = guild.me

    # 번호 계산과 채널 생성을 한 번에 묶어, 동시에 눌러도 번호가 겹치거나 방이 두 개 생기지 않게 합니다.
    async with _inquiry_lock(guild.id):
        gs = get_gs(guild.id)
        for existing in guild.text_channels:
            if _inquiry_owner_id(existing) == member.id:
                await interaction.followup.send(f"📨 이미 열려 있는 문의방이 있어요: {existing.mention}", ephemeral=True)
                return

        panel_channel = guild.get_channel(gs.get("inquiry_channel", 0))
        category = panel_channel.category if panel_channel else None
        number = int(gs.get("inquiry_count", 0)) + 1

        # 모두에게 숨기고, 문의한 사람 + 봇 + 지정한 열람 역할만 보이게 합니다.
        # 서버 관리자 권한을 가진 사람은 디스코드 규칙상 모든 채널을 자동으로 볼 수 있습니다.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: _grantable(me.guild_permissions, *_VIEWER_PERMS),
            me: _grantable(me.guild_permissions, "view_channel", "send_messages", "read_message_history",
                           "embed_links", "manage_channels", "mention_everyone"),
        }
        for role_id in gs.get("inquiry_roles", []):
            role = guild.get_role(role_id)
            if role and not role.is_default():
                overwrites[role] = _grantable(me.guild_permissions, *_VIEWER_PERMS)

        try:
            channel = await guild.create_text_channel(
                name=f"문의함-{number}",
                category=category,
                overwrites=overwrites,
                topic=f"{safe_text(member.display_name)}님의 문의방 | inquiry:{member.id}",
                reason=f"문의함: {member} 님이 문의방을 열었습니다.",
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ 봇에게 채널 관리 권한이 없어 문의방을 만들 수 없습니다. 관리자에게 알려주세요.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ 문의방을 만들지 못했습니다. 잠시 후 다시 시도해 주세요. (`{exc.status}`)", ephemeral=True)
            return
        gs["inquiry_count"] = number
        save_settings(GUILD_SETTINGS)

    # 문의함으로 지정한 채널 바로 아래로 옮깁니다. (실패해도 방은 정상 사용 가능)
    if panel_channel:
        try:
            await channel.move(after=panel_channel, reason="문의함 안내글 채널 바로 아래로 배치")
        except (discord.HTTPException, ValueError, TypeError) as exc:
            print(f"문의방 위치 이동 실패: {exc}")

    embed = discord.Embed(
        title="📨 문의가 시작되었습니다",
        description=(
            f"{member.mention}님,\n"
            f"궁금한 점이나 도움이 필요한 내용을\n"
            f"이 방에 편하게 남겨주세요.\n\n"
            f"{INQUIRY_WAIT_TEXT}"
        ),
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="이 방은 문의하신 분과 담당 관리자만 볼 수 있어요.")
    try:
        # 멘션은 임베드 안이 아니라 메시지 본문에 있어야 알림이 갑니다.
        # 비공개 방이라 @everyone 알림은 이 방을 볼 수 있는 사람에게만 갑니다.
        await channel.send(
            content="@everyone",
            embed=embed,
            view=InquiryCloseView(),
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
        )
    except discord.HTTPException as exc:
        try:
            await channel.delete(reason="문의방 안내 메시지 전송 실패로 정리")
        except discord.HTTPException:
            pass
        await interaction.followup.send(f"❌ 문의방에 안내 메시지를 보내지 못해 취소했습니다. 봇 권한을 확인해 주세요. (`{exc.status}`)", ephemeral=True)
        return
    # 완료 메시지는 남기지 않습니다. ('처리 중' 표시만 조용히 지웁니다.)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass

class InquiryOpenView(discord.ui.View):
    """안내글 아래의 '문의함 열기' 버튼 (봇을 재시작해도 동작하는 영구 버튼)"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="문의함 열기", emoji="📨", style=discord.ButtonStyle.primary, custom_id="natto:inquiry:open")
    async def open_inquiry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_inquiry_channel(interaction)

async def _close_ticket(interaction: discord.Interaction, owner_id: int, notice: str):
    """문의방/신고방 공통 닫기. 문의를 연 일반 유저만 닫을 수 없습니다."""
    channel = interaction.channel
    if not owner_id:
        await interaction.response.send_message("❌ 문의방 또는 신고방에서만 사용할 수 있습니다.", ephemeral=True)
        return
    if not _can_close_inquiry(interaction, channel, owner_id):
        await interaction.response.send_message("❌ 닫기는 관리자 또는 담당 역할을 가진 분만 할 수 있습니다.", ephemeral=True)
        return
    if channel.id in _CLOSING_INQUIRIES:
        await interaction.response.send_message("⏳ 이미 닫는 중입니다.", ephemeral=True)
        return
    _CLOSING_INQUIRIES.add(channel.id)
    try:
        await interaction.response.send_message(notice)
        await asyncio.sleep(INQUIRY_CLOSE_DELAY)
        await channel.delete(reason=f"문의/신고 종료 (닫은 사람: {interaction.user})")
        _LAST_REMINDER.pop(channel.id, None)
    except discord.HTTPException:
        try:
            await interaction.followup.send("❌ 채널을 닫지 못했습니다. 봇의 채널 관리 권한을 확인해 주세요.", ephemeral=True)
        except discord.HTTPException:
            pass
    finally:
        _CLOSING_INQUIRIES.discard(channel.id)

class InquiryCloseView(discord.ui.View):
    """문의방 안의 '문의 닫기' 버튼 (문의를 연 일반 유저를 제외한 모두가 사용 가능)"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="문의 닫기", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="natto:inquiry:close")
    async def close_inquiry(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _close_ticket(
            interaction, _inquiry_owner_id(interaction.channel),
            f"🔒 낫또봇이 {INQUIRY_CLOSE_DELAY}초뒤 문의를 중단합니다....",
        )

class InquiryPanelModal(discord.ui.Modal):
    """문의함 안내글(임베드)에 들어갈 내용을 입력받습니다. 제목은 고정이라 입력받지 않습니다."""
    def __init__(self, channel_id: int, role_ids: list):
        super().__init__(title="문의함 안내글 작성", timeout=900)
        self.channel_id = channel_id
        self.role_ids = role_ids
        self.body_input = discord.ui.TextInput(
            label="안내 내용", style=discord.TextStyle.paragraph, required=True, max_length=3000,
            placeholder="문의 전에 알아야 할 내용을 적어주세요. 줄바꿈은 그대로 입력하면 됩니다.",
        )
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id)
        if channel is None:
            await interaction.response.send_message("❌ 선택한 채널을 찾을 수 없습니다. 다시 `/설정 문의함`을 실행해 주세요.", ephemeral=True)
            return
        embed = discord.Embed(
            title=INQUIRY_PANEL_TITLE,
            description=self.body_input.value.strip(),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="아래 버튼을 눌러 문의함을 열어 주세요.")
        await interaction.response.defer(ephemeral=True)
        gs = get_gs(guild.id)
        try:
            message = await channel.send(embed=embed, view=InquiryOpenView())
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {channel.mention} 채널에 메시지를 보낼 권한이 없습니다. 봇 권한(메시지 보내기·링크 임베드)을 확인해 주세요.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ 안내글을 올리지 못했습니다. 잠시 후 다시 시도해 주세요. (`{exc.status}`)", ephemeral=True)
            return
        await _delete_inquiry_panel(guild, gs)  # 이전 안내글이 있으면 정리
        gs["inquiry_channel"] = channel.id
        gs["inquiry_panel_message"] = message.id
        gs["inquiry_roles"] = list(self.role_ids)
        save_settings(GUILD_SETTINGS)
        roles_text = " ".join(f"<@&{rid}>" for rid in self.role_ids)
        done = discord.Embed(
            title="✅ 문의함 설정 완료",
            description=(
                f"{channel.mention} 에 안내글과 **문의함 열기** 버튼을 올렸습니다.\n"
                f"[안내글 바로가기]({message.jump_url})"
            ),
            color=discord.Color.green(),
        )
        done.add_field(name="문의방을 볼 수 있는 역할", value=f"{roles_text}\n(서버 관리자는 항상 볼 수 있어요)", inline=False)
        await interaction.followup.send(embed=done, ephemeral=True)

class InquiryRoleSelect(discord.ui.RoleSelect):
    """2단계: 문의방을 볼 수 있는 역할 선택"""
    def __init__(self, channel_id: int):
        super().__init__(placeholder="문의방을 볼 수 있는 역할을 선택하세요 (최대 5개)", min_values=1, max_values=5)
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        roles = [r for r in self.values if not r.is_default()]
        if not roles:
            await interaction.response.send_message("❌ @everyone 은 선택할 수 없습니다. 담당 역할을 골라 주세요.", ephemeral=True)
            return
        await interaction.response.send_modal(InquiryPanelModal(self.channel_id, [r.id for r in roles]))

class InquiryChannelSelect(discord.ui.ChannelSelect):
    """1단계: 안내글을 올릴 채널 선택"""
    def __init__(self):
        super().__init__(placeholder="문의함 안내글을 올릴 채널을 선택하세요", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        view = discord.ui.View(timeout=180)
        view.add_item(InquiryRoleSelect(channel.id))
        await interaction.response.edit_message(
            content=(
                f"**문의함** 설정 (2/2): 문의방을 **볼 수 있는 역할**을 선택하세요.\n"
                f"안내글 채널: <#{channel.id}>\n"
                f"서버 관리자는 따로 고르지 않아도 항상 볼 수 있어요."
            ),
            view=view,
        )

# ====================================================================
# ⏰ 문의방 '닫기' 알림 (5분마다 대화 맨 아래로 다시 올림)
# ====================================================================
async def _send_inquiry_reminder(channel: discord.TextChannel):
    if discord.utils.utcnow() - channel.created_at < datetime.timedelta(seconds=INQUIRY_REMINDER_INTERVAL):
        return  # 방금 열린 방은 첫 안내 메시지에 닫기 버튼이 이미 있습니다.
    known = _LAST_REMINDER.get(channel.id)
    if known is None:
        # 봇이 재시작된 직후라면 방에 남아 있는 이전 알림을 찾아 정리합니다.
        old = []
        async for m in channel.history(limit=30):
            if m.author.id == bot.user.id and m.embeds and m.embeds[0].title == INQUIRY_REMINDER_TITLE:
                old.append(m)
        if old and old[0].id == channel.last_message_id:
            _LAST_REMINDER[channel.id] = old[0].id   # 이미 맨 아래에 있음
            return
        for m in old:
            try:
                await m.delete()
            except discord.HTTPException:
                pass
    else:
        if known == channel.last_message_id:
            return                                   # 아직 맨 아래에 있으니 다시 올리지 않음
        try:
            await channel.get_partial_message(known).delete()
        except discord.HTTPException:
            pass
    embed = discord.Embed(
        title=INQUIRY_REMINDER_TITLE,
        description="궁금증이 해결되었다면 아래 버튼으로 문의를 닫아 주세요.\n(닫기는 관리자와 담당 역할만 누를 수 있어요.)",
        color=discord.Color.light_grey(),
    )
    message = await channel.send(embed=embed, view=InquiryCloseView())
    _LAST_REMINDER[channel.id] = message.id

async def inquiry_reminder_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(INQUIRY_REMINDER_INTERVAL)
        for guild in list(bot.guilds):
            for channel in list(guild.text_channels):
                if not _inquiry_owner_id(channel):
                    continue
                try:
                    await _send_inquiry_reminder(channel)
                except discord.HTTPException:
                    continue
                except Exception as exc:
                    print(f"문의 닫기 알림 오류: {exc}")

# ====================================================================
# 🚨 신고함 (사유 작성 → 신고 대상 선택 → 신고자 + 제재 권한자만 보는 비공개 신고방)
# ====================================================================
REPORT_FEATURE = "신고함"
REPORT_PANEL_TITLE = "🚨 신고함"   # 안내글 제목은 고정입니다.
REPORT_REASON_FIELD = "📝 신고 사유"
REPORT_AGREE_TEXT = "동의를 하시면 신고를 당한 사용자는 제재가 들어가 서버이용이 일시적, 혹은 영구적으로 중지 될수 있습니다."
_REPORT_STAFF_PERMS = ("administrator", "moderate_members", "ban_members", "kick_members")
_REPORT_LOCKS = {}
_DECIDING_REPORTS = set()

def _report_lock(guild_id: int) -> asyncio.Lock:
    return _REPORT_LOCKS.setdefault(guild_id, asyncio.Lock())

def _report_info(channel):
    """신고방 주제(topic)에서 (신고자 ID, 신고 대상 ID)를 읽습니다. 신고방이 아니면 None."""
    match = re.search(r"report:(\d+):(\d+)", getattr(channel, "topic", None) or "")
    return (int(match.group(1)), int(match.group(2))) if match else None

def _is_report_staff(interaction: discord.Interaction) -> bool:
    perms = interaction.permissions
    return any(getattr(perms, name, False) for name in _REPORT_STAFF_PERMS)

def _report_staff_roles(guild: discord.Guild) -> list:
    """관리자·타임아웃·추방·차단 권한을 가진 역할(봇 연동 역할 제외)"""
    return [
        role for role in guild.roles
        if not role.is_default() and not role.managed
        and any(getattr(role.permissions, name, False) for name in _REPORT_STAFF_PERMS)
    ]

async def _delete_report_panel(guild: discord.Guild, gs: dict):
    channel = guild.get_channel(gs.get("report_channel", 0))
    message_id = gs.get("report_panel_message")
    if channel and message_id:
        try:
            await (await channel.fetch_message(message_id)).delete()
        except discord.HTTPException:
            pass

class ReportOpenView(discord.ui.View):
    """안내글 아래의 '신고함 열기' 버튼 (봇을 재시작해도 동작하는 영구 버튼)"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="신고함 열기", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="natto:report:open")
    async def open_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ 서버 안에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if not is_on(guild.id, REPORT_FEATURE):
            await interaction.response.send_message(INQUIRY_DISABLED_TEXT, ephemeral=True)
            return
        await interaction.response.send_modal(ReportReasonModal())

class ReportReasonModal(discord.ui.Modal):
    """1단계: 신고 사유 작성"""
    def __init__(self):
        super().__init__(title="신고 사유 작성", timeout=600)
        self.reason_input = discord.ui.TextInput(
            label="신고 사유", style=discord.TextStyle.paragraph, required=True, max_length=900,
            placeholder="누가, 언제, 어떤 일을 했는지 자세히 적어주세요.",
        )
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason_input.value.strip()
        if not reason:
            await interaction.response.send_message("❌ 신고 사유를 입력해 주세요.", ephemeral=True)
            return
        view = discord.ui.View(timeout=180)
        view.add_item(ReportTargetSelect(reason))
        await interaction.response.send_message(
            "**신고할 사람**을 선택하세요. 닉네임을 검색해 고른 뒤 Enter를 누르면 신고방이 열립니다.",
            view=view, ephemeral=True,
        )

class ReportTargetSelect(discord.ui.UserSelect):
    """2단계: 신고할 사람 선택"""
    def __init__(self, reason: str):
        super().__init__(placeholder="신고할 사람의 닉네임을 선택하세요", min_values=1, max_values=1)
        self.reason = reason

    async def callback(self, interaction: discord.Interaction):
        await create_report_room(interaction, self.values[0], self.reason)

async def create_report_room(interaction: discord.Interaction, picked, reason: str):
    guild = interaction.guild
    reporter = interaction.user
    if not is_on(guild.id, REPORT_FEATURE):
        await interaction.response.edit_message(content=INQUIRY_DISABLED_TEXT, view=None)
        return
    target = guild.get_member(picked.id)
    if target is None:
        await interaction.response.send_message("❌ 서버에서 찾을 수 없는 사용자입니다. 다른 사람을 선택해 주세요.", ephemeral=True)
        return
    if target.id == reporter.id:
        await interaction.response.send_message("❌ 자기 자신은 신고할 수 없습니다.", ephemeral=True)
        return
    if target.bot:
        await interaction.response.send_message("❌ 봇은 신고할 수 없습니다.", ephemeral=True)
        return

    await interaction.response.defer()
    me = guild.me
    async with _report_lock(guild.id):
        gs = get_gs(guild.id)
        for existing in guild.text_channels:
            if _report_info(existing) == (reporter.id, target.id):
                await interaction.edit_original_response(content=f"📨 이미 열려 있는 신고방이 있어요: {existing.mention}", view=None)
                return
        panel_channel = guild.get_channel(gs.get("report_channel", 0))
        category = panel_channel.category if panel_channel else None
        number = int(gs.get("report_count", 0)) + 1

        # 신고자 + 제재 권한을 가진 역할 + 봇만 보이게 하고, 신고 대상은 어떤 경우에도 숨깁니다.
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            reporter: _grantable(me.guild_permissions, *_VIEWER_PERMS),
            me: _grantable(me.guild_permissions, "view_channel", "send_messages", "read_message_history",
                           "embed_links", "manage_channels", "mention_everyone"),
        }
        for role in _report_staff_roles(guild):
            overwrites[role] = _grantable(me.guild_permissions, *_VIEWER_PERMS)
        overwrites[target] = discord.PermissionOverwrite(view_channel=False)

        try:
            channel = await guild.create_text_channel(
                name=f"신고함-{number}",
                category=category,
                overwrites=overwrites,
                topic=f"신고방 | report:{reporter.id}:{target.id}",
                reason=f"신고함: {reporter} 님이 신고방을 열었습니다.",
            )
        except discord.Forbidden:
            await interaction.edit_original_response(content="❌ 봇에게 채널 관리 권한이 없어 신고방을 만들 수 없습니다. 관리자에게 알려주세요.", view=None)
            return
        except discord.HTTPException as exc:
            await interaction.edit_original_response(content=f"❌ 신고방을 만들지 못했습니다. 잠시 후 다시 시도해 주세요. (`{exc.status}`)", view=None)
            return
        gs["report_count"] = number
        save_settings(GUILD_SETTINGS)

    if panel_channel:
        try:
            await channel.move(after=panel_channel, reason="신고함 안내글 채널 바로 아래로 배치")
        except (discord.HTTPException, ValueError, TypeError) as exc:
            print(f"신고방 위치 이동 실패: {exc}")

    embed = discord.Embed(title="🚨 신고가 접수되었습니다", color=discord.Color.red(), timestamp=discord.utils.utcnow())
    embed.add_field(name="👤 신고자", value=reporter.mention, inline=True)
    embed.add_field(name="🎯 신고 대상", value=target.mention, inline=True)
    embed.add_field(name=REPORT_REASON_FIELD, value=reason, inline=False)
    embed.add_field(name="⚠️ 동의 안내", value=REPORT_AGREE_TEXT, inline=False)
    embed.set_footer(text="이 방은 신고자와 제재 권한을 가진 관리자만 볼 수 있어요.")
    try:
        await channel.send(
            content="@everyone",
            embed=embed,
            view=ReportDecisionView(),
            allowed_mentions=discord.AllowedMentions(everyone=True, users=False, roles=False),
        )
    except discord.HTTPException as exc:
        try:
            await channel.delete(reason="신고방 안내 메시지 전송 실패로 정리")
        except discord.HTTPException:
            pass
        await interaction.edit_original_response(content=f"❌ 신고방에 안내 메시지를 보내지 못해 취소했습니다. 봇 권한을 확인해 주세요. (`{exc.status}`)", view=None)
        return
    await interaction.edit_original_response(content=f"✅ 신고방이 열렸어요: {channel.mention}", view=None)

async def _report_decide(interaction: discord.Interaction, agree: bool):
    guild = interaction.guild
    channel = interaction.channel
    message = interaction.message
    info = _report_info(channel)
    if guild is None or info is None or message is None:
        await interaction.response.send_message("❌ 신고방에서만 사용할 수 있습니다.", ephemeral=True)
        return
    reporter_id, target_id = info
    staff = _is_report_staff(interaction)
    if agree and not staff:
        await interaction.response.send_message("❌ 동의는 제재 권한(관리자·타임아웃·추방·차단)을 가진 분만 누를 수 있습니다.", ephemeral=True)
        return
    if not agree and not (staff or interaction.user.id == reporter_id):
        await interaction.response.send_message("❌ 거절은 신고자 본인 또는 제재 권한을 가진 분만 누를 수 있습니다.", ephemeral=True)
        return
    if message.id in _DECIDING_REPORTS:
        await interaction.response.send_message("⏳ 이미 처리 중입니다.", ephemeral=True)
        return

    embed0 = message.embeds[0] if message.embeds else None
    reason = next((f.value for f in (embed0.fields if embed0 else []) if f.name == REPORT_REASON_FIELD), "신고함으로 접수된 신고")

    if agree:
        # 제재 기능과 제재로그가 준비되어 있어야 기존 제재 기능으로 처리할 수 있습니다.
        problems = []
        if not is_on(guild.id, "제재"):
            problems.append("• **제재 기능**이 켜져 있지 않아요. `/활성화`에서 **제재**를 켜 주세요.")
        log_id = GUILD_SETTINGS.get(guild.id, {}).get("punish_log_channel")
        if not log_id or guild.get_channel(log_id) is None:
            problems.append("• **제재로그 채널**이 설정되어 있지 않아요. " + setup_hint(guild.id, "제재로그"))
        if problems:
            await interaction.response.send_message(
                "⚠️ 제재를 적용하려면 먼저 설정이 필요해요.\n" + "\n".join(problems) + "\n설정을 마친 뒤 다시 **동의**를 눌러 주세요.",
                ephemeral=True,
            )
            return

    _DECIDING_REPORTS.add(message.id)
    try:
        await interaction.response.defer()
        done = discord.Embed(timestamp=discord.utils.utcnow())
        finished = True
        if agree:
            target = guild.get_member(target_id)
            if target is None:
                done.title = "⚠️ 제재를 적용하지 못했습니다"
                done.description = f"<@{target_id}>님이 이미 서버에 없어 제재를 적용할 수 없습니다.\n처리자: {interaction.user.mention}"
                done.color = discord.Color.orange()
            else:
                # 제재 사유는 신고함에 적힌 내용 그대로 사용하고, 기존 /제재와 같은 제재 로직을 씁니다.
                ok = await punish_user(guild, target, reason, f"신고함 승인 ({interaction.user.name})")
                if ok:
                    done.title = "✅ 신고가 승인되었습니다"
                    done.description = f"{target.mention}님에게 제재가 적용되었습니다.\n처리자: {interaction.user.mention}"
                    done.add_field(name="제재 사유", value=reason[:1024], inline=False)
                    done.color = discord.Color.green()
                else:
                    finished = False
                    done.title = "❌ 제재 적용에 실패했습니다"
                    done.description = "봇의 역할 순위·권한을 확인한 뒤 다시 **동의**를 눌러 주세요. (자세한 내용은 제재로그에 남습니다)"
                    done.color = discord.Color.dark_red()
        else:
            done.title = "❌ 신고가 거절되었습니다"
            if staff:
                done.description = f"<@{reporter_id}>님의 신고가 거절되어 제재 없이 종료되었습니다.\n처리자: {interaction.user.mention}"
            else:
                done.description = f"<@{reporter_id}>님이 직접 신고를 거절(철회)해 제재 없이 종료되었습니다."
            done.color = discord.Color.red()

        if finished:
            disabled = ReportDecisionView()
            for child in disabled.children:
                child.disabled = True
            try:
                await interaction.edit_original_response(view=disabled)
            except discord.HTTPException:
                pass
        await channel.send(embed=done, view=ReportCloseView() if finished else None,
                           allowed_mentions=discord.AllowedMentions.none())
    finally:
        _DECIDING_REPORTS.discard(message.id)

class ReportDecisionView(discord.ui.View):
    """신고방의 동의/거절 버튼"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="동의", emoji="✅", style=discord.ButtonStyle.success, custom_id="natto:report:agree")
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _report_decide(interaction, True)

    @discord.ui.button(label="거절", emoji="❌", style=discord.ButtonStyle.danger, custom_id="natto:report:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _report_decide(interaction, False)

class ReportCloseView(discord.ui.View):
    """처리가 끝난 신고방의 '신고방 닫기' 버튼"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="신고방 닫기", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="natto:report:close")
    async def close_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        info = _report_info(interaction.channel)
        await _close_ticket(
            interaction, info[0] if info else 0,
            f"🔒 낫또봇이 {INQUIRY_CLOSE_DELAY}초뒤 신고방을 닫습니다....",
        )

class ReportPanelModal(discord.ui.Modal):
    """신고함 안내글(임베드)에 들어갈 내용을 입력받습니다. 제목은 고정입니다."""
    def __init__(self, channel_id: int):
        super().__init__(title="신고함 안내글 작성", timeout=900)
        self.channel_id = channel_id
        self.body_input = discord.ui.TextInput(
            label="안내 내용", style=discord.TextStyle.paragraph, required=True, max_length=3000,
            placeholder="신고 전에 알아야 할 내용을 적어주세요. 줄바꿈은 그대로 입력하면 됩니다.",
        )
        self.add_item(self.body_input)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id)
        if channel is None:
            await interaction.response.send_message("❌ 선택한 채널을 찾을 수 없습니다. 다시 `/설정 신고함`을 실행해 주세요.", ephemeral=True)
            return
        embed = discord.Embed(title=REPORT_PANEL_TITLE, description=self.body_input.value.strip(), color=discord.Color.red())
        embed.set_footer(text="아래 버튼을 눌러 신고함을 열어 주세요.")
        await interaction.response.defer(ephemeral=True)
        gs = get_gs(guild.id)
        try:
            message = await channel.send(embed=embed, view=ReportOpenView())
        except discord.Forbidden:
            await interaction.followup.send(f"❌ {channel.mention} 채널에 메시지를 보낼 권한이 없습니다. 봇 권한(메시지 보내기·링크 임베드)을 확인해 주세요.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ 안내글을 올리지 못했습니다. 잠시 후 다시 시도해 주세요. (`{exc.status}`)", ephemeral=True)
            return
        await _delete_report_panel(guild, gs)
        gs["report_channel"] = channel.id
        gs["report_panel_message"] = message.id
        save_settings(GUILD_SETTINGS)
        done = discord.Embed(
            title="✅ 신고함 설정 완료",
            description=f"{channel.mention} 에 안내글과 **신고함 열기** 버튼을 올렸습니다.\n[안내글 바로가기]({message.jump_url})",
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=done, ephemeral=True)

class ReportChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="신고함 안내글을 올릴 채널을 선택하세요", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ReportPanelModal(self.values[0].id))

# ====================================================================
# 📚 기능 카탈로그 (카테고리 · 명령어 매핑)
# ====================================================================
CATEGORIES = {
    "인증": ("🪪", "신입 유저 인증과 역할 지급"),
    "제재": ("🚨", "제재·검열·추방 관리"),
    "입퇴장": ("🚪", "유저 입장·퇴장 로그"),
    "기타": ("📖", "번역·가이드·청소·문의함·신고함"),
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
        "desc": "신입 입장 시 인증방에 보낼 안내 문구를 등록합니다. `@들어온사용자` `@자기자신` `(@닉네임)` 으로 멘션할 수 있어요.", "usage": "`메세지`",
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
        "cat": "기타", "desc": "현재 채널을 같은 설정으로 새로 만들어 메시지를 비웁니다. 고정 메시지·포스트는 복원됩니다.", "usage": "",
        "top": clean_channel_cb,
    },
    # ---------- 문의 ----------
    "문의함": {
        "cat": "기타", "label": "문의함 채널", "store": "inquiry_channel",
        "desc": "문의함 열기 버튼이 달린 안내글을 올립니다. 누르면 문의자·관리자·지정 역할만 보는 비공개 문의방이 열려요.",
        "usage": "`채널` → `열람 역할` → `안내 내용`",
        "set": make_set_channel("inquiry_channel", "문의함 채널"),
        "remove": make_remove_channel("inquiry_channel", "문의함 채널"),
        "check": make_check_channel("inquiry_channel", "문의함 채널"),
    },
    "신고함": {
        "cat": "기타", "label": "신고함 채널", "store": "report_channel",
        "desc": "신고함 열기 버튼이 달린 안내글을 올립니다. 신고하면 신고자와 제재 권한을 가진 관리자만 보는 비공개 신고방이 열려요.",
        "usage": "`채널` → `안내 내용`",
        "set": make_set_channel("report_channel", "신고함 채널"),
        "remove": make_remove_channel("report_channel", "신고함 채널"),
        "check": make_check_channel("report_channel", "신고함 채널"),
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
            placeholder = "@들어온사용자 · @자기자신 · (@닉네임) 으로 멘션할 수 있어요"
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
    elif 기능 == "신고함":
        view = discord.ui.View(timeout=180)
        view.add_item(ReportChannelSelect())
        await interaction.response.send_message("**신고함** 설정: 안내글과 **신고함 열기** 버튼을 올릴 채널을 선택하세요.", view=view, ephemeral=True)
    elif 기능 == "문의함":
        view = discord.ui.View(timeout=180)
        view.add_item(InquiryChannelSelect())
        await interaction.response.send_message("**문의함** 설정 (1/2): 안내글과 **문의함 열기** 버튼을 올릴 채널을 선택하세요.", view=view, ephemeral=True)
    elif 기능 == "인증지급":
        view = discord.ui.View(timeout=180)
        view.add_item(SettingRoleSelect(기능))
        await interaction.response.send_message("**인증지급** 설정: 아래에서 지급할 역할을 선택하세요.", view=view, ephemeral=True)
    elif 기능 in ("인증메세지", "제재단어", "검열단어"):
        await interaction.response.send_modal(SettingTextModal(기능))
    else:
        await interaction.response.send_message("❌ 이 항목은 설정 UI를 지원하지 않습니다.", ephemeral=True)

CHANNEL_SETTING_FEATURES = ("인증방", "제재로그", "입장로그", "퇴장로그", "문의함", "신고함")

def _check_embed(기능: str, description: str, ok: bool) -> discord.Embed:
    return discord.Embed(
        title=f"📋 {기능} 현재 설정",
        description=description[:4000],
        color=discord.Color.green() if ok else discord.Color.red(),
    )

@app_commands.describe(기능="확인할 기능을 선택하세요.")
@app_commands.autocomplete(기능=feature_autocomplete)
@app_commands.guild_only()
@admin_only
async def generic_check_cb(interaction: discord.Interaction, 기능: str):
    f = FEATURES.get(기능)
    if not f or "check" not in f:
        await interaction.response.send_message("❌ 확인할 수 있는 기능을 목록에서 선택해 주세요.", ephemeral=True)
        return
    guild = interaction.guild
    gs = GUILD_SETTINGS.get(guild.id, {})

    if 기능 in CHANNEL_SETTING_FEATURES:
        label = f["label"]
        channel_id = gs.get(f["store"])
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel:
            embed = _check_embed(기능, channel.mention, True)  # #채널명 링크로 표시됩니다.
            if 기능 in ("문의함", "신고함"):
                message_id = gs.get("inquiry_panel_message" if 기능 == "문의함" else "report_panel_message")
                if message_id:
                    embed.add_field(
                        name="안내글",
                        value=f"[바로가기](https://discord.com/channels/{guild.id}/{channel.id}/{message_id})",
                        inline=False,
                    )
            if 기능 == "문의함":
                viewers = [r for r in (guild.get_role(x) for x in gs.get("inquiry_roles", [])) if r]
                embed.add_field(
                    name="문의방을 볼 수 있는 역할",
                    value=(" ".join(r.mention for r in viewers) if viewers else "지정된 역할 없음 (서버 관리자만 볼 수 있어요)"),
                    inline=False,
                )
        elif channel_id:
            embed = _check_embed(기능, f"⚠️ 저장된 {label}을(를) 찾을 수 없습니다. (삭제되었거나 봇이 볼 수 없는 채널)\n`/제거 {기능}` 후 다시 설정해 주세요.", False)
        else:
            embed = _check_embed(기능, f"❌ 설정된 **{label}**{josa(label, '이', '가')} 없습니다.", False)
    elif 기능 == "인증지급":
        roles = [r for r in (guild.get_role(x) for x in gs.get("auth_role_ids", [])) if r]
        embed = _check_embed(기능, "\n".join(r.mention for r in roles) if roles else "❌ 설정된 인증지급 역할이 없습니다.", bool(roles))
    elif 기능 == "인증메세지":
        form = str(gs.get("auth_form", "")).replace("\\n", "\n").strip()
        embed = _check_embed(기능, form if form else "❌ 설정된 인증 안내 메세지가 없습니다.", bool(form))
        if form:
            preview = render_placeholders(form, guild, joined=interaction.user, me=interaction.user)
            embed.add_field(name="미리보기 (나를 들어온 사용자로 가정)", value=preview[:1024], inline=False)
    elif 기능 in ("제재단어", "검열단어"):
        words = [str(w) for w in gs.get("ban_words" if 기능 == "제재단어" else "censor_words", []) if str(w).strip()]
        embed = _check_embed(기능, " ".join(f"`{w}`" for w in words) if words else f"❌ 등록된 {기능}가 없습니다.", bool(words))
    else:
        embed = _check_embed(기능, "이 기능은 별도 설정값이 없습니다.", True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
    elif 기능 == "문의함" and gs.get("inquiry_channel"):
        await _delete_inquiry_panel(interaction.guild, gs)
        gs.pop("inquiry_channel", None); gs.pop("inquiry_panel_message", None); gs.pop("inquiry_roles", None)
        result = "🗑️ 문의함에 대한 안내글 등의 데이터를 제거합니다."
    elif 기능 == "신고함" and gs.get("report_channel"):
        await _delete_report_panel(interaction.guild, gs)
        gs.pop("report_channel", None); gs.pop("report_panel_message", None)
        result = "🗑️ 신고함에 대한 안내글 등의 데이터를 제거합니다."
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
        await interaction.response.send_message("❌ 초기화할 수 있는 목록을 선택해 주세요.", ephemeral=True)
        return
    # 목록 초기화는 ✅/❌ 2차 확인(make_reset)을 거친 뒤 실행됩니다.
    await FEATURES[기능]["reset"](interaction)

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

async def _server_admin_check(interaction: discord.Interaction) -> bool:
    """번역을 제외한 서버별 명령어는 실행 시에도 관리자 권한을 확인합니다."""
    return bool(interaction.guild and interaction.permissions.administrator)

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
        _with_default_permissions(app_commands.Command(name="역할주기", description="선택한 멤버에게 역할을 지급합니다.", callback=give_role_cb), admin_permissions),
    ]
    # 번역만 공개합니다. 나머지 기능 명령어는 모두 서버 관리자 권한이 있어야
    # 명령어 목록에 표시되고 실행할 수 있습니다. 봇 역할 순위와는 별개의 설정입니다.
    public_commands = {"번역"}
    for k, f in FEATURES.items():
        if k in enabled and "top" in f:
            command = app_commands.Command(
                name=k, description=f["desc"][:100], callback=f["top"]
            )
            if k not in public_commands:
                # 번역을 제외한 모든 기능 명령어는 관리자에게만 표시/사용 허용합니다.
                command = _with_default_permissions(command, admin_permissions)
            result.append(command)
    # 디스코드의 기본 표시 권한뿐 아니라 실행 시에도 서버 관리자 여부를 검사합니다.
    for command in result:
        if command.name != "번역":
            command.checks.append(_server_admin_check)
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
            await interaction.followup.send(f"❌ 전체 비활성화 중 명령어 갱신에 실패했습니다.\n`{e}`")
            return
    await interaction.followup.send(f"⛔ 전체 기능 {len(old)}개를 비활성화했습니다. 저장된 설정값은 유지됩니다.")

def build_deactivate_all_command() -> app_commands.Command:
    return _with_default_permissions(app_commands.Command(name="모두비활성화", description="모든 기능을 한 번에 비활성화합니다.", callback=deactivate_all_cb), discord.Permissions(administrator=True))

# ================= [ 안내 문구 멘션 치환 ] =================
_NICK_MENTION_RE = re.compile(r"\(@([^()\n@]{1,64})\)")

def _find_member_by_name(guild: discord.Guild, name: str):
    """서버 닉네임·본 닉네임(표시 이름/계정 이름)이 정확히 같은 멤버를 찾습니다. (대소문자 무시)"""
    wanted = name.strip().casefold()
    if not wanted:
        return None
    for m in guild.members:
        if wanted in (m.display_name.casefold(), m.name.casefold(), (m.global_name or "").casefold()):
            return m
    return None

def render_placeholders(text: str, guild: discord.Guild, joined=None, me=None) -> str:
    """안내 문구 속 멘션 자리표시자를 실제 멘션으로 바꿉니다.
    @들어온사용자 → 들어온 사용자 / @자기자신 → 자기 자신 / (@닉네임) → 해당 닉네임의 사용자"""
    if joined is not None:
        text = text.replace("@들어온사용자", joined.mention)
    if me is not None:
        text = text.replace("@자기자신", me.mention)

    def _replace(match):
        member = _find_member_by_name(guild, match.group(1))
        return member.mention if member else match.group(0)
    return _NICK_MENTION_RE.sub(_replace, text)

# ================= [ 입장 / 퇴장 ] =================
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    gs = GUILD_SETTINGS.get(guild.id, {})

    # 인증 안내 메세지 (자리표시자 치환)
    if not member.bot and is_on(guild.id, "인증방") and is_on(guild.id, "인증메세지"):
        channel = guild.get_channel(gs.get("auth_channel", 0))
        form = str(gs.get("auth_form", "")).replace("\\n", "\n").strip()
        if channel and form:
            text = render_placeholders(form, guild, joined=member, me=member)
            try:
                await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False))
            except discord.HTTPException as exc:
                print(f"인증 안내 메세지 전송 실패: {exc}")

    # 입장 로그
    if is_on(guild.id, "입장로그"):
        log_channel = guild.get_channel(gs.get("join_log_channel", 0))
        if log_channel:
            embed = discord.Embed(title="📥 유저 입장", description=f"{member.mention} ({safe_text(member.name)})", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            embed.add_field(name="계정 생성", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await log_channel.send(embed=embed)
            except discord.HTTPException as exc:
                print(f"입장 로그 전송 실패: {exc}")

@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    if not is_on(guild.id, "퇴장로그"):
        return
    log_channel = guild.get_channel(GUILD_SETTINGS.get(guild.id, {}).get("leave_log_channel", 0))
    if log_channel:
        embed = discord.Embed(title="📤 유저 퇴장", description=f"{member.mention} ({safe_text(member.name)})", color=discord.Color.dark_grey(), timestamp=discord.utils.utcnow())
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await log_channel.send(embed=embed)
        except discord.HTTPException as exc:
            print(f"퇴장 로그 전송 실패: {exc}")

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
