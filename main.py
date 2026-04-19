# =============================================================================
#  main.py  —  Discord Bot + Firebase Firestore + Flask uptime (Render-ready)
#  Painel Admin completo: Cadastrar / Editar / Apagar + Gestão de Admins
#  CORREÇÃO: todas as chamadas Firestore são feitas via asyncio.to_thread()
#            para não bloquear o event loop do Discord.
# =============================================================================

import asyncio
import io
import json
import logging
import os
import threading
from datetime import datetime

import discord
import firebase_admin
import pytz
from discord.ext import commands
from firebase_admin import credentials, firestore
from flask import Flask

# =============================================================================
#  CONFIGURAÇÃO  —  variáveis de ambiente
# =============================================================================
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
OWNER_ID: int      = int(os.getenv("OWNER_ID", "0"))
FIREBASE_JSON: str = os.getenv("FIREBASE_JSON", "")

if not DISCORD_TOKEN:
    raise RuntimeError("Variável de ambiente DISCORD_TOKEN não definida.")
if not OWNER_ID:
    raise RuntimeError("Variável de ambiente OWNER_ID não definida.")
if not FIREBASE_JSON:
    raise RuntimeError("Variável de ambiente FIREBASE_JSON não definida.")

TZ_BR          = pytz.timezone("America/Sao_Paulo")
DARK           = 0x2B2D31
COLECAO        = "scripts_dougo"
COLECAO_ADMINS = "admins_dougo"


# =============================================================================
#  FIREBASE  —  inicialização única
# =============================================================================
def _init_firebase() -> tuple:
    cred_dict: dict = json.loads(FIREBASE_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    return db.collection(COLECAO), db.collection(COLECAO_ADMINS)


_col, _col_admins = _init_firebase()


# =============================================================================
#  HELPERS
# =============================================================================
def _agora_br() -> str:
    return datetime.now(TZ_BR).strftime("%d/%m/%Y às %H:%M")


def _safe_filename(name: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return (clean.strip("_") or "arquivo")[:80]


# =============================================================================
#  CAMADA DE DADOS SÍNCRONA  —  chamadas puras ao Firestore
#  ⚠️  NUNCA chame estas funções diretamente em callbacks async.
#     Use sempre: await asyncio.to_thread(funcao, args...)
# =============================================================================

# ── scripts_dougo ─────────────────────────────────────────────────────────────
def _db_insert(nome: str, conteudo: str) -> None:
    _col.add({"nome": nome, "conteudo": conteudo, "data_criacao": _agora_br()})


def _db_all() -> list[dict]:
    docs = _col.order_by("data_criacao", direction=firestore.Query.DESCENDING).stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        d["doc_id"] = doc.id
        result.append(d)
    return result


def _db_get(doc_id: str) -> dict | None:
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict()
    d["doc_id"] = snap.id
    return d


def _db_update(doc_id: str, nome: str, conteudo: str) -> bool:
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return False
    _col.document(doc_id).update({
        "nome": nome,
        "conteudo": conteudo,
        "data_atualizacao": _agora_br(),
    })
    return True


def _db_delete(doc_id: str) -> bool:
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return False
    _col.document(doc_id).delete()
    return True


# ── admins_dougo ──────────────────────────────────────────────────────────────
def _admin_check(user_id: int) -> bool:
    """Verifica se o user_id existe na coleção de admins (síncrono)."""
    return _col_admins.document(str(user_id)).get().exists


def _admin_add(user_id: int, adicionado_por: int) -> None:
    _col_admins.document(str(user_id)).set({
        "user_id": user_id,
        "adicionado_por": adicionado_por,
        "data": _agora_br(),
    })


def _admin_remove(user_id: int) -> bool:
    snap = _col_admins.document(str(user_id)).get()
    if not snap.exists:
        return False
    _col_admins.document(str(user_id)).delete()
    return True


def _admin_all() -> list[dict]:
    return [doc.to_dict() for doc in _col_admins.stream()]


# =============================================================================
#  WRAPPERS ASSÍNCRONOS  —  use estes nas corrotinas do Discord
# =============================================================================
async def is_admin(user_id: int) -> bool:
    """OWNER_ID é sempre superadmin; outros verificados no Firestore em thread."""
    if user_id == OWNER_ID:
        return True
    return await asyncio.to_thread(_admin_check, user_id)


async def db_all() -> list[dict]:
    return await asyncio.to_thread(_db_all)


async def db_get(doc_id: str) -> dict | None:
    return await asyncio.to_thread(_db_get, doc_id)


async def db_insert(nome: str, conteudo: str) -> None:
    await asyncio.to_thread(_db_insert, nome, conteudo)


async def db_update(doc_id: str, nome: str, conteudo: str) -> bool:
    return await asyncio.to_thread(_db_update, doc_id, nome, conteudo)


async def db_delete(doc_id: str) -> bool:
    return await asyncio.to_thread(_db_delete, doc_id)


async def admin_all() -> list[dict]:
    return await asyncio.to_thread(_admin_all)


async def admin_add(user_id: int, adicionado_por: int) -> None:
    await asyncio.to_thread(_admin_add, user_id, adicionado_por)


async def admin_remove(user_id: int) -> bool:
    return await asyncio.to_thread(_admin_remove, user_id)


# =============================================================================
#  SERVIDOR FLASK  —  uptime na Render (porta 10000)
# =============================================================================
_flask_app = Flask(__name__)


@_flask_app.route("/")
def _health() -> tuple[str, int]:
    return "OK", 200


def _run_flask() -> None:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    _flask_app.run(host="0.0.0.0", port=10000)


# =============================================================================
#  ─── MODAIS ─────────────────────────────────────────────────────────────────
# =============================================================================

class CadastrarArquivoModal(discord.ui.Modal, title="➕ Cadastrar Arquivo"):
    nome = discord.ui.TextInput(
        label="Nome do Arquivo", placeholder="Ex: Hack Premium v3", max_length=100
    )
    conteudo = discord.ui.TextInput(
        label="Código / Conteúdo",
        style=discord.TextStyle.paragraph,
        placeholder="Cole aqui o texto ou código...",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await db_insert(str(self.nome), str(self.conteudo))
        embed = discord.Embed(
            title="✅ Arquivo Salvo no Firebase",
            description=(
                f"**Nome:** `{self.nome}`\n"
                f"**Em:** {_agora_br()}\n\n"
                "Use `/enviar_painel` para atualizar o menu público."
            ),
            color=DARK,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"❌ Erro: `{error}`", ephemeral=True)


class EditarArquivoModal(discord.ui.Modal):
    def __init__(self, doc_id: str, nome_atual: str, conteudo_atual: str) -> None:
        super().__init__(title="✏️ Editar Arquivo")
        self.doc_id = doc_id
        self.nome = discord.ui.TextInput(
            label="Nome do Arquivo", default=nome_atual[:100], max_length=100
        )
        self.conteudo = discord.ui.TextInput(
            label="Código / Conteúdo",
            style=discord.TextStyle.paragraph,
            default=conteudo_atual[:4000],
            max_length=4000,
        )
        self.add_item(self.nome)
        self.add_item(self.conteudo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await db_update(self.doc_id, str(self.nome), str(self.conteudo))
        if ok:
            embed = discord.Embed(
                title="✅ Arquivo Atualizado",
                description=f"**Nome:** `{self.nome}`\n**Atualizado em:** {_agora_br()}",
                color=DARK,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                "❌ Arquivo não encontrado (pode ter sido removido).", ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"❌ Erro: `{error}`", ephemeral=True)


class AdicionarAdminModal(discord.ui.Modal, title="👥 Adicionar Administrador"):
    user_id_input = discord.ui.TextInput(
        label="ID do Usuário Discord",
        placeholder="Ex: 123456789012345678",
        max_length=20,
        min_length=17,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Apenas o dono pode adicionar admins.", ephemeral=True
            )
            return
        try:
            novo_id = int(str(self.user_id_input).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ ID inválido. Insira apenas números.", ephemeral=True
            )
            return
        if novo_id == OWNER_ID:
            await interaction.response.send_message(
                "ℹ️ Este ID já é o dono do bot.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await admin_add(novo_id, interaction.user.id)
        embed = discord.Embed(
            title="✅ Admin Adicionado",
            description=f"**ID:** `{novo_id}` agora tem acesso ao painel.\n**Em:** {_agora_br()}",
            color=DARK,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"❌ Erro: `{error}`", ephemeral=True)


# =============================================================================
#  ─── SELECTS ADMINISTRATIVOS  (recebem dados já buscados — sem I/O no __init__)
# =============================================================================

class EditarArquivoSelect(discord.ui.Select):
    """Construído com a lista já carregada assincronamente pelo botão pai."""

    def __init__(self, arquivos: list[dict]) -> None:
        options = [
            discord.SelectOption(
                label=arq["nome"][:100],
                description=f"Atualizado: {arq.get('data_atualizacao', arq.get('data_criacao', '—'))}"[:100],
                value=arq["doc_id"],
            )
            for arq in arquivos[:25]
        ] or [discord.SelectOption(label="Nenhum arquivo cadastrado", value="none")]
        super().__init__(
            placeholder="✏️  Selecione o arquivo para editar...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum arquivo para editar.", ephemeral=True)
            return
        arq = await db_get(self.values[0])
        if not arq:
            await interaction.response.send_message("❌ Arquivo não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(
            EditarArquivoModal(arq["doc_id"], arq["nome"], arq["conteudo"])
        )


class ApagarArquivoSelect(discord.ui.Select):
    def __init__(self, arquivos: list[dict]) -> None:
        options = [
            discord.SelectOption(
                label=arq["nome"][:100],
                description=f"Criado: {arq.get('data_criacao', '—')}"[:100],
                value=arq["doc_id"],
            )
            for arq in arquivos[:25]
        ] or [discord.SelectOption(label="Nenhum arquivo cadastrado", value="none")]
        super().__init__(
            placeholder="🗑️  Selecione o arquivo para apagar...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum arquivo para apagar.", ephemeral=True)
            return
        arq = await db_get(self.values[0])
        if not arq:
            await interaction.response.send_message("❌ Arquivo não encontrado.", ephemeral=True)
            return
        view = ConfirmarApagarView(arq["doc_id"], arq["nome"])
        await interaction.response.send_message(
            f"⚠️ Tem certeza que deseja apagar **{arq['nome'][:80]}**?\n**Esta ação é irreversível.**",
            view=view,
            ephemeral=True,
        )


class RemoverAdminSelect(discord.ui.Select):
    def __init__(self, admins: list[dict]) -> None:
        options = [
            discord.SelectOption(
                label=f"ID: {a['user_id']}",
                description=f"Adicionado em: {a.get('data', '—')}"[:100],
                value=str(a["user_id"]),
            )
            for a in admins[:25]
        ] or [discord.SelectOption(label="Nenhum admin cadastrado", value="none")]
        super().__init__(
            placeholder="🗑️  Selecione o admin para remover...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Apenas o dono pode remover admins.", ephemeral=True
            )
            return
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum admin para remover.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok = await admin_remove(int(self.values[0]))
        if ok:
            await interaction.followup.send(
                f"✅ ID `{self.values[0]}` removido dos administradores.", ephemeral=True
            )
        else:
            await interaction.followup.send("❌ Admin não encontrado.", ephemeral=True)


# =============================================================================
#  ─── VIEWS AUXILIARES ────────────────────────────────────────────────────────
# =============================================================================

class ConfirmarApagarView(discord.ui.View):
    def __init__(self, doc_id: str, nome: str) -> None:
        super().__init__(timeout=60)
        self.doc_id = doc_id
        self.nome   = nome

    @discord.ui.button(label="✅ Confirmar Exclusão", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.edit_message(content="❌ Acesso negado.", view=None)
            return
        await interaction.response.defer()
        ok = await db_delete(self.doc_id)
        msg = (
            f"✅ Arquivo **{self.nome[:80]}** apagado com sucesso."
            if ok else "❌ Arquivo não encontrado (já removido?)."
        )
        await interaction.edit_original_response(content=msg, view=None)

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="🚫 Exclusão cancelada.", view=None)


class GerenciarAdminsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(label="➕ Adicionar Admin", style=discord.ButtonStyle.primary, row=0)
    async def adicionar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Apenas o dono pode adicionar admins.", ephemeral=True
            )
            return
        await interaction.response.send_modal(AdicionarAdminModal())

    @discord.ui.button(label="🗑️ Remover Admin", style=discord.ButtonStyle.danger, row=0)
    async def remover(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Apenas o dono pode remover admins.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        admins = await admin_all()
        if not admins:
            await interaction.followup.send("ℹ️ Nenhum admin cadastrado.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(RemoverAdminSelect(admins))
        await interaction.followup.send(view=view, ephemeral=True)


# =============================================================================
#  ─── PAINEL ADMIN PRINCIPAL ──────────────────────────────────────────────────
# =============================================================================
class AdminPainelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    # ── Row 0 ──────────────────────────────────────────────────────────────────
    @discord.ui.button(label="➕ Cadastrar", style=discord.ButtonStyle.success,
                       custom_id="admin_cadastrar", row=0)
    async def cadastrar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.send_modal(CadastrarArquivoModal())

    @discord.ui.button(label="✏️ Editar", style=discord.ButtonStyle.primary,
                       custom_id="admin_editar", row=0)
    async def editar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        arquivos = await db_all()
        if not arquivos:
            await interaction.followup.send("ℹ️ Nenhum arquivo cadastrado para editar.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(EditarArquivoSelect(arquivos))
        await interaction.followup.send(
            "Selecione o arquivo que deseja **editar**:", view=view, ephemeral=True
        )

    @discord.ui.button(label="🗑️ Apagar", style=discord.ButtonStyle.danger,
                       custom_id="admin_apagar", row=0)
    async def apagar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        arquivos = await db_all()
        if not arquivos:
            await interaction.followup.send("ℹ️ Nenhum arquivo cadastrado para apagar.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(ApagarArquivoSelect(arquivos))
        await interaction.followup.send(
            "Selecione o arquivo que deseja **apagar**:", view=view, ephemeral=True
        )

    # ── Row 1 ──────────────────────────────────────────────────────────────────
    @discord.ui.button(label="📋 Listar Arquivos", style=discord.ButtonStyle.secondary,
                       custom_id="admin_listar", row=1)
    async def listar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        arquivos = await db_all()
        if not arquivos:
            await interaction.followup.send("ℹ️ Nenhum arquivo cadastrado.", ephemeral=True)
            return
        linhas = []
        for arq in arquivos:
            atualizado = arq.get("data_atualizacao")
            data_label = f"✏️ {atualizado}" if atualizado else f"📅 {arq.get('data_criacao', '—')}"
            linhas.append(f"• **{arq['nome'][:55]}**\n  └ `{arq['doc_id'][:12]}…`  {data_label}")
        texto = "\n".join(linhas)
        if len(texto) > 3800:
            texto = texto[:3797] + "..."
        embed = discord.Embed(title="📋 Arquivos — scripts_dougo", description=texto, color=DARK)
        embed.set_footer(text=f"Total: {len(arquivos)} arquivo(s)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="👥 Gerenciar Admins", style=discord.ButtonStyle.secondary,
                       custom_id="admin_admins", row=1)
    async def gerenciar_admins(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Apenas o **dono** do bot pode gerenciar administradores.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        admins = await admin_all()
        linhas = (
            "\n".join(
                f"• `{a['user_id']}` — adicionado em _{a.get('data', '—')}_"
                for a in admins
            )
            or "_Nenhum admin extra. Apenas você (OWNER) tem acesso._"
        )
        embed = discord.Embed(title="👥 Administradores do Painel", color=DARK)
        embed.add_field(name="IDs com acesso ao /gerenciar", value=linhas[:1024], inline=False)
        embed.set_footer(text=f"Total: {len(admins)} admin(s) extra(s)  •  OWNER não listado")
        await interaction.followup.send(embed=embed, view=GerenciarAdminsView(), ephemeral=True)


# =============================================================================
#  ─── SELECT PERSISTENTE — painel público ────────────────────────────────────
# =============================================================================
class ArquivoSelect(discord.ui.Select):
    """
    Em Persistent Views o __init__ é chamado com options vazias no on_ready.
    O callback busca os dados em tempo real no Firestore via asyncio.to_thread.
    """

    def __init__(self) -> None:
        # Placeholder option — as opções reais são buscadas no callback
        super().__init__(
            custom_id="persistent_arquivo_select",
            placeholder="📂  Escolha o arquivo para receber na DM...",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Carregando...", value="loading")],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        # Busca os dados em tempo real (sem bloquear o event loop)
        arquivos = await db_all()

        # Se o usuário selecionou "loading" (painel antigo sem opções reais)
        # ou não há arquivos, informa e sai
        if not arquivos:
            await interaction.followup.send(
                "ℹ️ Nenhum arquivo disponível no momento.", ephemeral=True
            )
            return

        # Reconstrói o mapa doc_id → arquivo para lookup rápido
        mapa = {arq["doc_id"]: arq for arq in arquivos}
        valor = self.values[0]

        if valor == "loading" or valor not in mapa:
            # O painel estava com options antigas — manda lista atualizada
            opcoes_texto = "\n".join(
                f"• **{arq['nome'][:60]}**" for arq in arquivos[:15]
            )
            await interaction.followup.send(
                "⚠️ O painel precisa ser **atualizado**.\n"
                "Peça ao admin para usar /enviar_painel novamente.\n\n"
                f"**Arquivos disponíveis:**\n{opcoes_texto}",
                ephemeral=True,
            )
            return

        arq = mapa[valor]
        buf = io.BytesIO(arq["conteudo"].encode("utf-8"))
        arquivo = discord.File(buf, filename=f"{_safe_filename(arq['nome'])}.txt")

        try:
            await interaction.user.send(
                content=f"📄 Aqui está seu arquivo — cadastrado em **{arq.get('data_criacao', '—')}**.",
                file=arquivo,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Não consigo te enviar o arquivo via DM.\n"
                "Ative **Mensagens Diretas** nas configurações de privacidade do servidor.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "❌ Falha ao enviar o arquivo. Tente novamente em instantes.", ephemeral=True
            )
            return

        await interaction.followup.send("✅ Arquivo enviado na sua DM com segurança!", ephemeral=True)


class PainelView(discord.ui.View):
    """Persistent View — registrada no on_ready com timeout=None."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ArquivoSelect())


# =============================================================================
#  ─── BOT ─────────────────────────────────────────────────────────────────────
# =============================================================================
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook() -> None:
    await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    bot.add_view(PainelView())
    print(f"✅ Bot online como {bot.user}  (ID: {bot.user.id})")
    print("✅ Slash commands sincronizados.")
    print("✅ Persistent View registrada.")
    print(f"🔥 Firestore — coleções: '{COLECAO}' / '{COLECAO_ADMINS}'")


# ---------------------------------------------------------------------------
#  /gerenciar
# ---------------------------------------------------------------------------
@bot.tree.command(name="gerenciar", description="Painel administrativo completo")
async def cmd_gerenciar(interaction: discord.Interaction) -> None:
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ Você não tem permissão para acessar o painel.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    arquivos = await db_all()
    admins   = await admin_all()

    embed = discord.Embed(title="⚙️  Painel de Controle", color=DARK)
    embed.add_field(
        name="📊 Status",
        value=(
            f"🗂️ **Arquivos:** {len(arquivos)}\n"
            f"👥 **Admins extra:** {len(admins)}\n"
            f"🕐 **Agora:** {_agora_br()}"
        ),
        inline=False,
    )
    embed.add_field(
        name="🛠️ Ações disponíveis",
        value=(
            "**➕ Cadastrar** — Novo arquivo\n"
            "**✏️ Editar** — Alterar nome ou conteúdo\n"
            "**🗑️ Apagar** — Remover com confirmação\n"
            "**📋 Listar** — Ver todos os arquivos\n"
            "**👥 Admins** — Gerenciar permissões _(dono)_"
        ),
        inline=False,
    )
    embed.set_footer(text="DOUGOBRASIL • Enterprise  •  Firebase Firestore")
    await interaction.followup.send(embed=embed, view=AdminPainelView(), ephemeral=True)


# ---------------------------------------------------------------------------
#  /enviar_painel
# ---------------------------------------------------------------------------
@bot.tree.command(name="enviar_painel", description="Envia e fixa o painel de arquivos no canal atual")
async def cmd_enviar_painel(interaction: discord.Interaction) -> None:
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
        return
    canal = interaction.channel
    if not isinstance(canal, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("❌ Use em um canal de texto.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Busca arquivos para popular o Select com opções reais
    arquivos = await db_all()

    # Monta a View com opções reais do banco
    view = discord.ui.View(timeout=None)
    select = ArquivoSelect()
    if arquivos:
        select.options = [
            discord.SelectOption(
                label=arq["nome"][:100],
                description=f"Cadastrado em: {arq.get('data_criacao', '—')}"[:100],
                value=arq["doc_id"],
            )
            for arq in arquivos[:25]
        ]
    else:
        select.options = [
            discord.SelectOption(
                label="Nenhum arquivo disponível",
                description="Aguarde o administrador cadastrar conteúdos.",
                value="loading",
            )
        ]
    view.add_item(select)

    embed = discord.Embed(
        title="📦  Repositório de Arquivos",
        description=(
            "Selecione um item no menu abaixo para receber o arquivo **diretamente na sua DM**.\n\n"
            "> ⚠️ Certifique-se de que suas **Mensagens Diretas** estão abertas "
            "nas configurações de privacidade do servidor."
        ),
        color=DARK,
    )
    embed.set_footer(text="DOUGOBRASIL • Enterprise  •  Powered by Firebase")

    try:
        msg = await canal.send(embed=embed, view=view)
    except discord.Forbidden:
        await interaction.followup.send("❌ Sem permissão neste canal.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.followup.send("❌ Falha ao publicar.", ephemeral=True)
        return

    pinned = False
    try:
        await msg.pin(reason="Painel fixo do repositório")
        pinned = True
    except (discord.Forbidden, discord.HTTPException):
        pass

    status = "e fixado" if pinned else "(sem permissão de pin)"
    await interaction.followup.send(f"✅ Painel enviado {status} em {canal.mention}.", ephemeral=True)


# =============================================================================
#  ENTRY POINT
# =============================================================================
def main() -> None:
    t = threading.Thread(target=_run_flask, daemon=True, name="flask-uptime")
    t.start()
    print("🌐 Flask iniciado na porta 10000.")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
