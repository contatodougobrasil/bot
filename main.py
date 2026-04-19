# =============================================================================
#  main.py  —  Discord Bot + Firebase Firestore + Flask uptime (Render-ready)
#  Autor: refatorado por Antigravity (Senior Python / Cloud DB)
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
from google.cloud.firestore_v1 import CollectionReference

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

# Fuso horário oficial do Brasil
TZ_BR = pytz.timezone("America/Sao_Paulo")

# Cor dark-mode para todos os Embeds
DARK = 0x2B2D31

# Nome da coleção no Firestore
COLECAO = "scripts_dougo"


# =============================================================================
#  FIREBASE  —  inicialização via JSON da variável de ambiente
# =============================================================================
def _init_firebase() -> CollectionReference:
    """
    Lê o conteúdo JSON da variável FIREBASE_JSON, cria as credenciais
    e retorna a referência da coleção Firestore.
    """
    cred_dict: dict = json.loads(FIREBASE_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    return db.collection(COLECAO)


_col: CollectionReference = _init_firebase()


# =============================================================================
#  HELPERS DE DATA / HORA
# =============================================================================
def _agora_br() -> str:
    """Retorna a data/hora atual no fuso de São Paulo, formatada."""
    agora = datetime.now(TZ_BR)
    return agora.strftime("%d/%m/%Y às %H:%M")


# =============================================================================
#  CAMADA DE DADOS  —  Firestore
# =============================================================================

def db_insert(nome: str, conteudo: str) -> None:
    """Salva um novo documento na coleção Firestore."""
    _col.add({
        "nome": nome,
        "conteudo": conteudo,
        "data_criacao": _agora_br(),
    })


def db_all() -> list[dict]:
    """
    Retorna todos os documentos da coleção, ordenados por data de criação
    (mais recentes primeiro).  Cada item inclui a chave 'doc_id'.
    """
    docs = _col.order_by(
        "data_criacao", direction=firestore.Query.DESCENDING
    ).stream()
    result = []
    for doc in docs:
        data = doc.to_dict()
        data["doc_id"] = doc.id
        result.append(data)
    return result


def db_get(doc_id: str) -> dict | None:
    """Busca um único documento pelo ID."""
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return None
    data = snap.to_dict()
    data["doc_id"] = snap.id
    return data


def db_delete(doc_id: str) -> bool:
    """Remove um documento. Retorna True se encontrado e removido."""
    snap = _col.document(doc_id).get()
    if not snap.exists:
        return False
    _col.document(doc_id).delete()
    return True


# =============================================================================
#  SERVIDOR FLASK  —  mantém o serviço vivo na Render (porta 10000)
# =============================================================================
_flask_app = Flask(__name__)


@_flask_app.route("/")
def _health() -> tuple[str, int]:
    return "OK", 200


def _run_flask() -> None:
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)
    _flask_app.run(host="0.0.0.0", port=10000)


# =============================================================================
#  HELPERS  GERAIS
# =============================================================================

def _safe_filename(name: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return (clean.strip("_") or "arquivo")[:80]


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# =============================================================================
#  MODAL  —  Cadastro de arquivo
# =============================================================================

class CadastrarArquivoModal(discord.ui.Modal, title="Cadastrar Arquivo"):
    nome = discord.ui.TextInput(
        label="Nome do Arquivo",
        placeholder="Ex: Hack Premium v3",
        max_length=100,
    )
    conteudo = discord.ui.TextInput(
        label="Código / Conteúdo do Arquivo",
        style=discord.TextStyle.paragraph,
        placeholder="Cole aqui o texto ou código...",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not _is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return

        db_insert(str(self.nome), str(self.conteudo))

        embed = discord.Embed(
            title="✅ Arquivo Salvo no Firebase",
            description=(
                f"**Nome:** `{self.nome}`\n"
                f"**Cadastrado em:** {_agora_br()}\n\n"
                "Use `/enviar_painel` no canal público para atualizar o menu."
            ),
            color=DARK,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            f"❌ Erro interno: `{error}`", ephemeral=True
        )


# =============================================================================
#  SELECT  PERSISTENTE  —  painel público (busca em tempo real no Firestore)
# =============================================================================

class ArquivoSelect(discord.ui.Select):
    def __init__(self) -> None:
        arquivos = db_all()          # busca em tempo real ao montar a View
        options: list[discord.SelectOption] = []

        for arq in arquivos[:25]:
            options.append(
                discord.SelectOption(
                    label=arq["nome"][:100],
                    description=f"Cadastrado em: {arq.get('data_criacao', '—')}"[:100],
                    value=arq["doc_id"],
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum arquivo disponível",
                    description="Aguarde o administrador cadastrar conteúdos.",
                    value="none",
                )
            ]

        super().__init__(
            custom_id="persistent_arquivo_select",
            placeholder="📂  Escolha o arquivo para receber na DM...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message(
                "ℹ️ Nenhum arquivo cadastrado ainda.", ephemeral=True
            )
            return

        arq = db_get(self.values[0])
        if not arq:
            await interaction.response.send_message(
                "❌ Arquivo não encontrado (pode ter sido removido).", ephemeral=True
            )
            return

        nome = arq["nome"]
        conteudo = arq["conteudo"]
        data_criacao = arq.get("data_criacao", "—")

        buf = io.BytesIO(conteudo.encode("utf-8"))
        arquivo = discord.File(buf, filename=f"{_safe_filename(nome)}.txt")

        try:
            await interaction.user.send(
                content=f"📄 Aqui está seu arquivo — cadastrado em **{data_criacao}**.",
                file=arquivo,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Não consigo te enviar o arquivo via DM.\n"
                "Ative **Mensagens Diretas** nas configurações de privacidade do servidor e tente novamente.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Falha ao enviar o arquivo. Tente novamente em instantes.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "✅ Arquivo enviado na sua DM com segurança!", ephemeral=True
        )


class PainelView(discord.ui.View):
    """View persistente — nunca expira, registrada no on_ready."""

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
    """Sincroniza slash commands antes do login."""
    await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    """Registra a PainelView como persistente no startup."""
    bot.add_view(PainelView())
    print(f"✅ Bot online como {bot.user}  (ID: {bot.user.id})")
    print("✅ Slash commands sincronizados.")
    print("✅ Persistent View registrada.")
    print(f"🔥 Firestore conectado — coleção: '{COLECAO}'")


# ---------------------------------------------------------------------------
#  /gerenciar  —  painel administrativo (somente OWNER_ID)
# ---------------------------------------------------------------------------
@bot.tree.command(name="gerenciar", description="Painel exclusivo do administrador")
async def cmd_gerenciar(interaction: discord.Interaction) -> None:
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
        return

    arquivos = db_all()
    linhas = "\n".join(
        f"• `{arq['doc_id'][:6]}…` — **{arq['nome'][:55]}** — _{arq.get('data_criacao', '—')}_"
        for arq in arquivos
    ) or "_Nenhum arquivo cadastrado._"

    if len(linhas) > 3800:
        linhas = linhas[:3797] + "..."

    embed = discord.Embed(title="⚙️  Painel de Controle", color=DARK)
    embed.add_field(name="🔥 Firebase · scripts_dougo", value=linhas, inline=False)
    embed.add_field(
        name="➕ Cadastrar novo arquivo",
        value="Clique no botão abaixo para abrir o formulário.",
        inline=False,
    )
    embed.set_footer(text=f"Total: {len(arquivos)} arquivo(s)  •  Fuso: America/Sao_Paulo")

    view = discord.ui.View(timeout=300)

    async def _abrir_modal(inter: discord.Interaction) -> None:
        await inter.response.send_modal(CadastrarArquivoModal())

    btn = discord.ui.Button(
        label="➕ Cadastrar Arquivo",
        style=discord.ButtonStyle.primary,
        custom_id="admin_cadastrar_btn",
    )
    btn.callback = _abrir_modal
    view.add_item(btn)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
#  /enviar_painel  —  publica e fixa o painel público no canal
# ---------------------------------------------------------------------------
@bot.tree.command(name="enviar_painel", description="Envia e fixa o painel de arquivos no canal atual")
async def cmd_enviar_painel(interaction: discord.Interaction) -> None:
    if not _is_owner(interaction.user.id):
        await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
        return

    canal = interaction.channel
    if not isinstance(canal, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "❌ Use este comando em um canal de texto.", ephemeral=True
        )
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
        await interaction.followup.send(
            "❌ Sem permissão para enviar mensagens neste canal.", ephemeral=True
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "❌ Falha ao publicar o painel. Tente novamente.", ephemeral=True
        )
        return

    pinned = False
    try:
        await msg.pin(reason="Painel fixo do repositório de arquivos")
        pinned = True
    except (discord.Forbidden, discord.HTTPException):
        pass

    status = "e fixado" if pinned else "(pin falhou — conceda **Gerenciar mensagens** ao bot)"
    await interaction.followup.send(
        f"✅ Painel enviado {status} em {canal.mention}.", ephemeral=True
    )


# =============================================================================
#  ENTRY POINT
# =============================================================================
def main() -> None:
    # Flask em thread daemon para manter o uptime na Render
    t = threading.Thread(target=_run_flask, daemon=True, name="flask-uptime")
    t.start()
    print("🌐 Flask iniciado na porta 10000.")

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
