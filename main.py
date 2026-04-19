# =============================================================================
#  main.py  —  Discord Bot + Flask uptime server (Render-ready)
#  Autor: refatorado por Antigravity (Senior Python / Infra)
# =============================================================================

import io
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from flask import Flask

# =============================================================================
#  CONFIGURAÇÃO  —  variáveis de ambiente (sem config.py)
# =============================================================================
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))
DATABASE_PATH: Path = Path("database.db")

if not DISCORD_TOKEN:
    raise RuntimeError("A variável de ambiente DISCORD_TOKEN não está definida.")
if not OWNER_ID:
    raise RuntimeError("A variável de ambiente OWNER_ID não está definida.")


# =============================================================================
#  SERVIDOR FLASK  —  mantém o serviço ativo na Render (porta 10000)
# =============================================================================
_flask_app = Flask(__name__)


@_flask_app.route("/")
def _health() -> tuple[str, int]:
    return "OK", 200


def _run_flask() -> None:
    """Inicia o Flask em modo silencioso numa thread bloqueante."""
    import logging
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)          # suprime logs verbosos do Flask
    _flask_app.run(host="0.0.0.0", port=10000)


# =============================================================================
#  BANCO DE DADOS  —  SQLite  (tabela: arquivos)
# =============================================================================

def init_db() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS arquivos (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                nome         TEXT    NOT NULL,
                conteudo     TEXT    NOT NULL,
                data_criacao TEXT    NOT NULL
            )
            """
        )
        conn.commit()


def db_insert(nome: str, conteudo: str) -> None:
    data_criacao = datetime.now().strftime("%d/%m/%Y %H:%M")
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT INTO arquivos (nome, conteudo, data_criacao) VALUES (?, ?, ?)",
            (nome, conteudo, data_criacao),
        )
        conn.commit()


def db_all() -> list[tuple[int, str, str, str]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        return conn.execute(
            "SELECT id, nome, conteudo, data_criacao FROM arquivos ORDER BY id DESC"
        ).fetchall()


def db_get(arquivo_id: int) -> tuple[int, str, str, str] | None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        return conn.execute(
            "SELECT id, nome, conteudo, data_criacao FROM arquivos WHERE id = ?",
            (arquivo_id,),
        ).fetchone()


def db_delete(arquivo_id: int) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        cur = conn.execute("DELETE FROM arquivos WHERE id = ?", (arquivo_id,))
        conn.commit()
        return cur.rowcount > 0


# =============================================================================
#  HELPERS
# =============================================================================
DARK = 0x2B2D31   # cor dark-mode para todos os embeds


def _safe_filename(name: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return (clean.strip("_") or "arquivo")[:80]


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


# =============================================================================
#  MODAIS
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
            await interaction.response.send_message(
                "❌ Acesso negado.", ephemeral=True
            )
            return

        db_insert(str(self.nome), str(self.conteudo))

        embed = discord.Embed(
            title="✅ Arquivo Cadastrado",
            description=(
                f"**Nome:** `{self.nome}`\n"
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
#  SELECT PERSISTENTE  —  painel público
# =============================================================================

class ArquivoSelect(discord.ui.Select):
    def __init__(self) -> None:
        arquivos = db_all()
        options: list[discord.SelectOption] = []

        for arq_id, nome, _, data_criacao in arquivos[:25]:
            options.append(
                discord.SelectOption(
                    label=nome[:100],
                    description=f"Cadastrado em: {data_criacao}"[:100],
                    value=str(arq_id),
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

        arq = db_get(int(self.values[0]))
        if not arq:
            await interaction.response.send_message(
                "❌ Arquivo não encontrado (pode ter sido removido).", ephemeral=True
            )
            return

        _, nome, conteudo, data_criacao = arq
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
    """Executado antes do login — inicializa DB e sincroniza slash commands."""
    init_db()
    await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    """Registra a PainelView como persistente logo na inicialização."""
    bot.add_view(PainelView())
    print(f"✅ Bot online como {bot.user}  (ID: {bot.user.id})")
    print("✅ Slash commands sincronizados.")
    print("✅ Persistent View registrada.")


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
        f"• **#{pid}** — {nome[:60]} — _{dh}_"
        for pid, nome, _, dh in arquivos
    ) or "_Nenhum arquivo cadastrado._"

    if len(linhas) > 3800:
        linhas = linhas[:3797] + "..."

    embed = discord.Embed(
        title="⚙️  Painel de Controle",
        color=DARK,
    )
    embed.add_field(
        name="📋 Arquivos no banco",
        value=linhas,
        inline=False,
    )
    embed.add_field(
        name="➕ Cadastrar novo arquivo",
        value="Clique no botão abaixo para abrir o formulário.",
        inline=False,
    )
    embed.set_footer(text=f"Total: {len(arquivos)} arquivo(s)")

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
#  /enviar_painel  —  publica e fixa o painel público
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
            "> ⚠️ Certifique-se de que suas **Mensagens Diretas** estão abertas nas configurações de privacidade do servidor."
        ),
        color=DARK,
    )
    embed.set_footer(text="DOUGOBRASIL • Enterprise  •  Painel persistente")

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

    # Tenta fixar — falha silenciosamente se não tiver permissão
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
    # Inicia o Flask numa thread daemon antes do event loop do Discord
    t = threading.Thread(target=_run_flask, daemon=True, name="flask-uptime")
    t.start()
    print("🌐 Flask iniciado na porta 10000.")

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
