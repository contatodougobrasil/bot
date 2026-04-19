# =============================================================================
#  main.py  —  Discord Bot + Firebase Firestore + Flask uptime (Render-ready)
#  Painel Admin completo: Cadastrar / Editar / Apagar + Gestão de Admins
# =============================================================================

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
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))
FIREBASE_JSON: str = os.getenv("FIREBASE_JSON", "")

if not DISCORD_TOKEN:
    raise RuntimeError("Variável de ambiente DISCORD_TOKEN não definida.")
if not OWNER_ID:
    raise RuntimeError("Variável de ambiente OWNER_ID não definida.")
if not FIREBASE_JSON:
    raise RuntimeError("Variável de ambiente FIREBASE_JSON não definida.")

TZ_BR   = pytz.timezone("America/Sao_Paulo")
DARK    = 0x2B2D31       # cor dark-mode
COLECAO         = "scripts_dougo"
COLECAO_ADMINS  = "admins_dougo"


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
#  HELPERS  —  data/hora e nome de arquivo seguro
# =============================================================================
def _agora_br() -> str:
    return datetime.now(TZ_BR).strftime("%d/%m/%Y às %H:%M")


def _safe_filename(name: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return (clean.strip("_") or "arquivo")[:80]


# =============================================================================
#  CAMADA DE DADOS  —  scripts_dougo
# =============================================================================
def db_insert(nome: str, conteudo: str) -> None:
    _col.add({"nome": nome, "conteudo": conteudo, "data_criacao": _agora_br()})


def db_all() -> list[dict]:
    docs = _col.order_by("data_criacao", direction=firestore.Query.DESCENDING).stream()
    result = []
    for doc in docs:
        d = doc.to_dict()
        d["doc_id"] = doc.id
        result.append(d)
    return result


def db_get(doc_id: str) -> dict | None:
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return None
    d = snap.to_dict()
    d["doc_id"] = snap.id
    return d


def db_update(doc_id: str, nome: str, conteudo: str) -> bool:
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return False
    _col.document(doc_id).update({
        "nome": nome,
        "conteudo": conteudo,
        "data_atualizacao": _agora_br(),
    })
    return True


def db_delete(doc_id: str) -> bool:
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return False
    _col.document(doc_id).delete()
    return True


# =============================================================================
#  CAMADA DE DADOS  —  admins_dougo
# =============================================================================
def admin_add(user_id: int, adicionado_por: int) -> None:
    _col_admins.document(str(user_id)).set({
        "user_id": user_id,
        "adicionado_por": adicionado_por,
        "data": _agora_br(),
    })


def admin_remove(user_id: int) -> bool:
    snap = _col_admins.document(str(user_id)).get()
    if not snap.exists:
        return False
    _col_admins.document(str(user_id)).delete()
    return True


def admin_all() -> list[dict]:
    return [doc.to_dict() for doc in _col_admins.stream()]


def _is_admin(user_id: int) -> bool:
    """OWNER_ID é sempre superadmin; outros são verificados no Firestore."""
    if user_id == OWNER_ID:
        return True
    return _col_admins.document(str(user_id)).get().exists


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

# ── Cadastrar ────────────────────────────────────────────────────────────────
class CadastrarArquivoModal(discord.ui.Modal, title="➕ Cadastrar Arquivo"):
    nome = discord.ui.TextInput(
        label="Nome do Arquivo",
        placeholder="Ex: Hack Premium v3",
        max_length=100,
    )
    conteudo = discord.ui.TextInput(
        label="Código / Conteúdo",
        style=discord.TextStyle.paragraph,
        placeholder="Cole aqui o texto ou código...",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        db_insert(str(self.nome), str(self.conteudo))
        embed = discord.Embed(
            title="✅ Arquivo Salvo no Firebase",
            description=(
                f"**Nome:** `{self.nome}`\n"
                f"**Em:** {_agora_br()}\n\n"
                "Use `/enviar_painel` para atualizar o menu público."
            ),
            color=DARK,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"❌ Erro: `{error}`", ephemeral=True)


# ── Editar (pré-preenchido) ───────────────────────────────────────────────────
class EditarArquivoModal(discord.ui.Modal):
    def __init__(self, doc_id: str, nome_atual: str, conteudo_atual: str) -> None:
        super().__init__(title="✏️ Editar Arquivo")
        self.doc_id = doc_id

        self.nome = discord.ui.TextInput(
            label="Nome do Arquivo",
            default=nome_atual[:100],
            max_length=100,
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
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        ok = db_update(self.doc_id, str(self.nome), str(self.conteudo))
        if ok:
            embed = discord.Embed(
                title="✅ Arquivo Atualizado",
                description=(
                    f"**Nome:** `{self.nome}`\n"
                    f"**Atualizado em:** {_agora_br()}"
                ),
                color=DARK,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                "❌ Arquivo não encontrado (pode ter sido removido).", ephemeral=True
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"❌ Erro: `{error}`", ephemeral=True)


# ── Adicionar Admin ───────────────────────────────────────────────────────────
class AdicionarAdminModal(discord.ui.Modal, title="👥 Adicionar Administrador"):
    user_id_input = discord.ui.TextInput(
        label="ID do Usuário Discord",
        placeholder="Ex: 123456789012345678",
        max_length=20,
        min_length=17,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Apenas o dono pode adicionar admins.", ephemeral=True)
            return
        try:
            novo_id = int(str(self.user_id_input).strip())
        except ValueError:
            await interaction.response.send_message("❌ ID inválido. Insira apenas números.", ephemeral=True)
            return

        if novo_id == OWNER_ID:
            await interaction.response.send_message("ℹ️ Este ID já é o dono do bot.", ephemeral=True)
            return

        admin_add(novo_id, interaction.user.id)
        embed = discord.Embed(
            title="✅ Admin Adicionado",
            description=f"**ID:** `{novo_id}` agora tem acesso ao painel admin.\n**Em:** {_agora_br()}",
            color=DARK,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"❌ Erro: `{error}`", ephemeral=True)


# =============================================================================
#  ─── SELECTS ADMINISTRATIVOS ─────────────────────────────────────────────────
# =============================================================================

# ── Select: escolher arquivo para EDITAR ─────────────────────────────────────
class EditarArquivoSelect(discord.ui.Select):
    def __init__(self) -> None:
        arquivos = db_all()
        options = [
            discord.SelectOption(
                label=arq["nome"][:100],
                description=f"Atualizado: {arq.get('data_atualizacao', arq.get('data_criacao', '—'))}"[:100],
                value=arq["doc_id"],
            )
            for arq in arquivos[:25]
        ] or [
            discord.SelectOption(label="Nenhum arquivo cadastrado", value="none")
        ]
        super().__init__(
            placeholder="✏️  Selecione o arquivo para editar...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum arquivo para editar.", ephemeral=True)
            return
        arq = db_get(self.values[0])
        if not arq:
            await interaction.response.send_message("❌ Arquivo não encontrado.", ephemeral=True)
            return
        await interaction.response.send_modal(
            EditarArquivoModal(arq["doc_id"], arq["nome"], arq["conteudo"])
        )


# ── Select: escolher arquivo para APAGAR ─────────────────────────────────────
class ApagarArquivoSelect(discord.ui.Select):
    def __init__(self) -> None:
        arquivos = db_all()
        options = [
            discord.SelectOption(
                label=arq["nome"][:100],
                description=f"Criado: {arq.get('data_criacao', '—')}"[:100],
                value=arq["doc_id"],
            )
            for arq in arquivos[:25]
        ] or [
            discord.SelectOption(label="Nenhum arquivo cadastrado", value="none")
        ]
        super().__init__(
            placeholder="🗑️  Selecione o arquivo para apagar...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum arquivo para apagar.", ephemeral=True)
            return
        arq = db_get(self.values[0])
        if not arq:
            await interaction.response.send_message("❌ Arquivo não encontrado.", ephemeral=True)
            return
        # Pede confirmação antes de apagar
        view = ConfirmarApagarView(arq["doc_id"], arq["nome"])
        await interaction.response.send_message(
            f"⚠️ Tem certeza que deseja apagar **{arq['nome'][:80]}**?\n"
            "**Esta ação é irreversível.**",
            view=view,
            ephemeral=True,
        )


# ── Select: remover admin ─────────────────────────────────────────────────────
class RemoverAdminSelect(discord.ui.Select):
    def __init__(self) -> None:
        admins = admin_all()
        options = [
            discord.SelectOption(
                label=f"ID: {a['user_id']}",
                description=f"Adicionado em: {a.get('data', '—')}"[:100],
                value=str(a["user_id"]),
            )
            for a in admins[:25]
        ] or [
            discord.SelectOption(label="Nenhum admin cadastrado", value="none")
        ]
        super().__init__(
            placeholder="🗑️  Selecione o admin para remover...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Apenas o dono pode remover admins.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum admin para remover.", ephemeral=True)
            return
        uid = int(self.values[0])
        ok = admin_remove(uid)
        if ok:
            await interaction.response.send_message(
                f"✅ ID `{uid}` removido dos administradores.", ephemeral=True
            )
        else:
            await interaction.response.send_message("❌ Admin não encontrado.", ephemeral=True)


# =============================================================================
#  ─── VIEWS AUXILIARES ────────────────────────────────────────────────────────
# =============================================================================

# ── Confirmação de exclusão ───────────────────────────────────────────────────
class ConfirmarApagarView(discord.ui.View):
    def __init__(self, doc_id: str, nome: str) -> None:
        super().__init__(timeout=60)
        self.doc_id = doc_id
        self.nome   = nome

    @discord.ui.button(label="✅ Confirmar Exclusão", style=discord.ButtonStyle.danger)
    async def confirmar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.edit_message(content="❌ Acesso negado.", view=None)
            return
        ok = db_delete(self.doc_id)
        msg = (
            f"✅ Arquivo **{self.nome[:80]}** apagado com sucesso."
            if ok else "❌ Arquivo não encontrado (já removido?)."
        )
        await interaction.response.edit_message(content=msg, view=None)

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="🚫 Exclusão cancelada.", view=None)


# ── Painel de gestão de admins ────────────────────────────────────────────────
class GerenciarAdminsView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(label="➕ Adicionar Admin", style=discord.ButtonStyle.primary, row=0)
    async def adicionar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Apenas o dono pode adicionar admins.", ephemeral=True)
            return
        await interaction.response.send_modal(AdicionarAdminModal())

    @discord.ui.button(label="🗑️ Remover Admin", style=discord.ButtonStyle.danger, row=0)
    async def remover(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("❌ Apenas o dono pode remover admins.", ephemeral=True)
            return
        admins = admin_all()
        if not admins:
            await interaction.response.send_message("ℹ️ Nenhum admin cadastrado.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(RemoverAdminSelect())
        await interaction.response.send_message(view=view, ephemeral=True)


# =============================================================================
#  ─── PAINEL ADMIN PRINCIPAL ──────────────────────────────────────────────────
# =============================================================================
class AdminPainelView(discord.ui.View):
    """View principal do painel administrativo — expira em 10 min."""

    def __init__(self) -> None:
        super().__init__(timeout=600)

    # ── Row 0 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(label="➕ Cadastrar", style=discord.ButtonStyle.success,
                       custom_id="admin_cadastrar", row=0)
    async def cadastrar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        await interaction.response.send_modal(CadastrarArquivoModal())

    @discord.ui.button(label="✏️ Editar", style=discord.ButtonStyle.primary,
                       custom_id="admin_editar", row=0)
    async def editar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        arquivos = db_all()
        if not arquivos:
            await interaction.response.send_message("ℹ️ Nenhum arquivo cadastrado para editar.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(EditarArquivoSelect())
        await interaction.response.send_message(
            "Selecione o arquivo que deseja **editar**:", view=view, ephemeral=True
        )

    @discord.ui.button(label="🗑️ Apagar", style=discord.ButtonStyle.danger,
                       custom_id="admin_apagar", row=0)
    async def apagar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        arquivos = db_all()
        if not arquivos:
            await interaction.response.send_message("ℹ️ Nenhum arquivo cadastrado para apagar.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(ApagarArquivoSelect())
        await interaction.response.send_message(
            "Selecione o arquivo que deseja **apagar**:", view=view, ephemeral=True
        )

    # ── Row 1 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(label="📋 Listar Arquivos", style=discord.ButtonStyle.secondary,
                       custom_id="admin_listar", row=1)
    async def listar(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not _is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        arquivos = db_all()
        if not arquivos:
            await interaction.response.send_message("ℹ️ Nenhum arquivo cadastrado.", ephemeral=True)
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
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="👥 Gerenciar Admins", style=discord.ButtonStyle.secondary,
                       custom_id="admin_admins", row=1)
    async def gerenciar_admins(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "❌ Apenas o **dono** do bot pode gerenciar administradores.", ephemeral=True
            )
            return

        admins = admin_all()
        if admins:
            linhas = "\n".join(
                f"• `{a['user_id']}` — adicionado em _{a.get('data', '—')}_"
                for a in admins
            )
        else:
            linhas = "_Nenhum admin extra cadastrado. Apenas você (OWNER) tem acesso._"

        embed = discord.Embed(title="👥 Administradores do Painel", color=DARK)
        embed.add_field(name="IDs com acesso ao /gerenciar", value=linhas[:1024], inline=False)
        embed.set_footer(text=f"Total: {len(admins)} admin(s) extra(s)  •  OWNER não listado")

        await interaction.response.send_message(
            embed=embed, view=GerenciarAdminsView(), ephemeral=True
        )


# =============================================================================
#  ─── SELECT PERSISTENTE — painel público ────────────────────────────────────
# =============================================================================
class ArquivoSelect(discord.ui.Select):
    def __init__(self) -> None:
        arquivos = db_all()
        options: list[discord.SelectOption] = [
            discord.SelectOption(
                label=arq["nome"][:100],
                description=f"Cadastrado em: {arq.get('data_criacao', '—')}"[:100],
                value=arq["doc_id"],
            )
            for arq in arquivos[:25]
        ] or [
            discord.SelectOption(
                label="Nenhum arquivo disponível",
                description="Aguarde o administrador cadastrar conteúdos.",
                value="none",
            )
        ]
        super().__init__(
            custom_id="persistent_arquivo_select",
            placeholder="📂  Escolha o arquivo para receber na DM...",
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message("ℹ️ Nenhum arquivo cadastrado ainda.", ephemeral=True)
            return

        arq = db_get(self.values[0])
        if not arq:
            await interaction.response.send_message(
                "❌ Arquivo não encontrado (pode ter sido removido).", ephemeral=True
            )
            return

        buf = io.BytesIO(arq["conteudo"].encode("utf-8"))
        arquivo = discord.File(buf, filename=f"{_safe_filename(arq['nome'])}.txt")

        try:
            await interaction.user.send(
                content=f"📄 Aqui está seu arquivo — cadastrado em **{arq.get('data_criacao', '—')}**.",
                file=arquivo,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Não consigo te enviar o arquivo via DM.\n"
                "Ative **Mensagens Diretas** nas configurações de privacidade do servidor.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Falha ao enviar o arquivo. Tente novamente em instantes.", ephemeral=True
            )
            return

        await interaction.response.send_message("✅ Arquivo enviado na sua DM com segurança!", ephemeral=True)


class PainelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ArquivoSelect())


# =============================================================================
#  BOT
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
    print("✅ Persistent View (painel público) registrada.")
    print(f"🔥 Firestore conectado — coleções: '{COLECAO}' / '{COLECAO_ADMINS}'")


# ---------------------------------------------------------------------------
#  /gerenciar  —  painel administrativo completo
# ---------------------------------------------------------------------------
@bot.tree.command(name="gerenciar", description="Painel administrativo completo")
async def cmd_gerenciar(interaction: discord.Interaction) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ Você não tem permissão para acessar o painel de administração.", ephemeral=True
        )
        return

    arquivos = db_all()
    admins   = admin_all()

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
            "**➕ Cadastrar** — Novo arquivo no Firebase\n"
            "**✏️ Editar** — Alterar nome ou conteúdo\n"
            "**🗑️ Apagar** — Remover com confirmação\n"
            "**📋 Listar** — Ver todos os arquivos\n"
            "**👥 Admins** — Adicionar / Remover permissões _(dono)_"
        ),
        inline=False,
    )
    embed.set_footer(text="DOUGOBRASIL • Enterprise  •  Firebase Firestore")

    await interaction.response.send_message(embed=embed, view=AdminPainelView(), ephemeral=True)


# ---------------------------------------------------------------------------
#  /enviar_painel  —  publica e fixa o painel público no canal
# ---------------------------------------------------------------------------
@bot.tree.command(name="enviar_painel", description="Envia e fixa o painel de arquivos no canal atual")
async def cmd_enviar_painel(interaction: discord.Interaction) -> None:
    if not _is_admin(interaction.user.id):
        await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
        return

    canal = interaction.channel
    if not isinstance(canal, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("❌ Use este comando em um canal de texto.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

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
        msg = await canal.send(embed=embed, view=PainelView())
    except discord.Forbidden:
        await interaction.followup.send("❌ Sem permissão para enviar mensagens neste canal.", ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.followup.send("❌ Falha ao publicar o painel.", ephemeral=True)
        return

    pinned = False
    try:
        await msg.pin(reason="Painel fixo do repositório")
        pinned = True
    except (discord.Forbidden, discord.HTTPException):
        pass

    status = "e fixado" if pinned else "(pin falhou — conceda **Gerenciar mensagens** ao bot)"
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
