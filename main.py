import io
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

from config import DISCORD_TOKEN

# ---------------------------------------------------------------------------
# Servidor Flask — necessário para hospedar na Render (health-check HTTP)
# ---------------------------------------------------------------------------
app_flask = Flask(__name__)


@app_flask.route("/")
def health_check():
    return "OK", 200


def run_flask() -> None:
    """Inicia o Flask na porta 8080 (blocking). Deve ser chamado em uma thread separada."""
    app_flask.run(host="0.0.0.0", port=8080)
# ---------------------------------------------------------------------------

# Substitua pelo seu ID de usuário no Discord
OWNER_ID = 1494514417734254592
DATABASE_PATH = Path("database.db")


def init_database() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                conteudo_codigo TEXT NOT NULL,
                data_hora TEXT
            )
            """
        )
        columns = [row[1] for row in conn.execute("PRAGMA table_info(produtos)").fetchall()]
        if "data_hora" not in columns:
            conn.execute("ALTER TABLE produtos ADD COLUMN data_hora TEXT")
        conn.commit()


def add_produto(nome: str, conteudo_codigo: str) -> None:
    data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            "INSERT INTO produtos (nome, conteudo_codigo, data_hora) VALUES (?, ?, ?)",
            (nome, conteudo_codigo, data_hora),
        )
        conn.commit()


def get_all_produtos() -> list[tuple[int, str, str, str | None]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            "SELECT id, nome, conteudo_codigo, data_hora FROM produtos ORDER BY id DESC"
        )
        return cursor.fetchall()


def get_produto_by_id(produto_id: int) -> tuple[int, str, str, str | None] | None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        cursor = conn.execute(
            "SELECT id, nome, conteudo_codigo, data_hora FROM produtos WHERE id = ?",
            (produto_id,),
        )
        return cursor.fetchone()


def update_produto(produto_id: int, nome: str, conteudo_codigo: str) -> bool:
    """Atualiza nome, conteúdo e redefine data_hora para o momento da edição."""
    data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
    with sqlite3.connect(DATABASE_PATH) as conn:
        cur = conn.execute(
            "UPDATE produtos SET nome = ?, conteudo_codigo = ?, data_hora = ? WHERE id = ?",
            (nome, conteudo_codigo, data_hora, produto_id),
        )
        conn.commit()
        return cur.rowcount > 0


def delete_produto(produto_id: int) -> bool:
    with sqlite3.connect(DATABASE_PATH) as conn:
        cur = conn.execute("DELETE FROM produtos WHERE id = ?", (produto_id,))
        conn.commit()
        return cur.rowcount > 0


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def sanitize_filename(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
    cleaned = cleaned.strip("_")
    return cleaned[:80] or "arquivo"


class AddProdutoModal(discord.ui.Modal, title="Adicionar Novo Produto"):
    nome_produto = discord.ui.TextInput(
        label="Nome do Produto",
        placeholder="Ex: Pacote Premium v1",
        max_length=100,
    )
    codigo_script = discord.ui.TextInput(
        label="Código/Script",
        style=discord.TextStyle.paragraph,
        placeholder="Cole o conteúdo do arquivo...",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "❌ Acesso negado. Apenas o administrador pode cadastrar conteúdo.",
                ephemeral=True,
            )
            return

        add_produto(str(self.nome_produto), str(self.codigo_script))
        await interaction.response.send_message(
            "✅ Produto cadastrado com sucesso.\n"
            "💡 **Dica:** reenvie `/enviar_painel` no canal público para o menu refletir novos itens.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            f"❌ Erro ao salvar produto: {error}",
            ephemeral=True,
        )


class EditProdutoModal(discord.ui.Modal):
    def __init__(self, produto_id: int, nome_atual: str, conteudo_atual: str) -> None:
        super().__init__(title=f"Editar produto #{produto_id}")
        self.produto_id = produto_id

        nome_default = nome_atual[:100]
        if len(conteudo_atual) <= 4000:
            conteudo_default = conteudo_atual
        else:
            conteudo_default = conteudo_atual[:3997] + "..."

        self.nome_produto = discord.ui.TextInput(
            label="Nome do Produto",
            default=nome_default,
            max_length=100,
        )
        self.codigo_script = discord.ui.TextInput(
            label="Código/Script",
            style=discord.TextStyle.paragraph,
            default=conteudo_default,
            max_length=4000,
        )
        self.add_item(self.nome_produto)
        self.add_item(self.codigo_script)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "❌ Acesso negado.",
                ephemeral=True,
            )
            return

        ok = update_produto(
            self.produto_id,
            str(self.nome_produto),
            str(self.codigo_script),
        )
        if ok:
            await interaction.response.send_message(
                "✅ Produto atualizado.\n"
                "💡 Reenvie `/enviar_painel` no canal público para atualizar o menu fixo.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Produto não encontrado (pode ter sido excluído).",
                ephemeral=True,
            )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(
            f"❌ Erro ao editar: {error}",
            ephemeral=True,
        )


class AdminEditarSelect(discord.ui.Select):
    def __init__(self) -> None:
        produtos = get_all_produtos()[:25]
        options: list[discord.SelectOption] = []
        for pid, nome, _, dh in produtos:
            options.append(
                discord.SelectOption(
                    label=f"#{pid} {nome[:80]}",
                    description=(dh or "sem data")[:100],
                    value=str(pid),
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum produto cadastrado",
                    value="none",
                    description="Cadastre um item antes.",
                )
            ]
        super().__init__(
            placeholder="Escolha o produto para editar...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message(
                "ℹ️ Não há produtos para editar.",
                ephemeral=True,
            )
            return
        pid = int(self.values[0])
        row = get_produto_by_id(pid)
        if not row:
            await interaction.response.send_message(
                "❌ Produto não encontrado.",
                ephemeral=True,
            )
            return
        _, nome, conteudo, _ = row
        await interaction.response.send_modal(EditProdutoModal(pid, nome, conteudo))


class AdminExcluirSelect(discord.ui.Select):
    def __init__(self) -> None:
        produtos = get_all_produtos()[:25]
        options: list[discord.SelectOption] = []
        for pid, nome, _, dh in produtos:
            options.append(
                discord.SelectOption(
                    label=f"#{pid} {nome[:80]}",
                    description=(dh or "sem data")[:100],
                    value=str(pid),
                )
            )
        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum produto cadastrado",
                    value="none",
                    description="Nada para excluir.",
                )
            ]
        super().__init__(
            placeholder="Escolha o produto para excluir...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message("❌ Acesso negado.", ephemeral=True)
            return
        if self.values[0] == "none":
            await interaction.response.send_message(
                "ℹ️ Não há produtos para excluir.",
                ephemeral=True,
            )
            return
        pid = int(self.values[0])
        row = get_produto_by_id(pid)
        if not row:
            await interaction.response.send_message(
                "❌ Produto não encontrado.",
                ephemeral=True,
            )
            return
        nome = row[1]
        if delete_produto(pid):
            await interaction.response.send_message(
                f"✅ Produto **#{pid} — {nome[:80]}** excluído.\n"
                "💡 Reenvie `/enviar_painel` no canal público para atualizar o menu fixo.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Não foi possível excluir.",
                ephemeral=True,
            )


class ProdutoSelect(discord.ui.Select):
    def __init__(self) -> None:
        produtos = get_all_produtos()
        options: list[discord.SelectOption] = []
        for produto_id, nome, _, data_hora in produtos[:25]:
            descricao = f"Cadastrado em: {data_hora}" if data_hora else "Sem data registrada"
            options.append(
                discord.SelectOption(
                    label=nome[:100],
                    description=descricao[:100],
                    value=str(produto_id),
                )
            )

        if not options:
            options = [
                discord.SelectOption(
                    label="Nenhum produto disponível",
                    description="Aguarde o administrador cadastrar conteúdos.",
                    value="none",
                )
            ]

        super().__init__(
            custom_id="persistent_produto_select",
            placeholder="Escolha o arquivo para resgatar...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.send_message(
                "ℹ️ Ainda não há arquivos cadastrados.",
                ephemeral=True,
            )
            return

        produto_id = int(self.values[0])
        produto = get_produto_by_id(produto_id)
        if not produto:
            await interaction.response.send_message(
                "❌ Arquivo não encontrado. Tente novamente.",
                ephemeral=True,
            )
            return

        _, nome, conteudo_codigo, data_hora = produto
        arquivo_memoria = io.BytesIO(conteudo_codigo.encode("utf-8"))
        arquivo = discord.File(arquivo_memoria, filename=f"{sanitize_filename(nome)}.txt")

        try:
            await interaction.user.send(
                f"Aqui está seu arquivo. Cadastrado em: {data_hora or 'Data não registrada'}",
                file=arquivo,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Não consegui te enviar o arquivo. Por favor, ative suas Mensagens Diretas nas configurações de privacidade do servidor e tente novamente!",
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
            "✅ Arquivo enviado no seu privado com segurança!",
            ephemeral=True,
        )


class PersistentDropdownView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(ProdutoSelect())


class AdminControlView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @discord.ui.button(
        label="➕ Cadastrar",
        style=discord.ButtonStyle.primary,
        custom_id="admin_cadastrar_novo_conteudo",
        row=0,
    )
    async def cadastrar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "❌ Acesso negado. Comando restrito ao administrador.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(AddProdutoModal())

    @discord.ui.button(
        label="✏️ Editar",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_editar_conteudo",
        row=0,
    )
    async def editar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "❌ Acesso negado.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=300)
        view.add_item(AdminEditarSelect())
        await interaction.response.send_message(
            "Selecione abaixo o produto que deseja **editar**:",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="🗑️ Excluir",
        style=discord.ButtonStyle.danger,
        custom_id="admin_excluir_conteudo",
        row=0,
    )
    async def excluir(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "❌ Acesso negado.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=300)
        view.add_item(AdminExcluirSelect())
        await interaction.response.send_message(
            "Selecione abaixo o produto que deseja **excluir** (ação imediata):",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="📋 Listar",
        style=discord.ButtonStyle.secondary,
        custom_id="admin_listar_conteudos",
        row=1,
    )
    async def listar(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not is_owner(interaction.user.id):
            await interaction.response.send_message(
                "❌ Acesso negado.",
                ephemeral=True,
            )
            return
        produtos = get_all_produtos()
        if not produtos:
            await interaction.response.send_message(
                "ℹ️ Nenhum produto cadastrado.",
                ephemeral=True,
            )
            return
        linhas = []
        for pid, nome, _, dh in produtos:
            linhas.append(f"• **#{pid}** — {nome[:80]} — _{dh or 'sem data'}_")
        texto = "\n".join(linhas)
        if len(texto) > 3800:
            texto = texto[:3797] + "..."
        embed = discord.Embed(
            title="📋 Produtos no banco",
            description=texto,
            color=discord.Color.dark_grey(),
        )
        embed.set_footer(text=f"Total: {len(produtos)} • até 25 no menu público")
        await interaction.response.send_message(embed=embed, ephemeral=True)


def build_painel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📦 REPOSITÓRIO DE ARQUIVOS",
        description="Selecione um item no menu para receber o arquivo em sua DM.",
        color=discord.Color.dark_grey(),
    )
    embed.set_footer(
        text="⚠️ Certifique-se de que suas mensagens privadas (DMs) estão abertas para receber os conteúdos."
    )
    return embed


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
_view_registered = False


@bot.event
async def setup_hook() -> None:
    init_database()
    await bot.tree.sync()


@bot.event
async def on_ready() -> None:
    global _view_registered
    if not _view_registered:
        bot.add_view(PersistentDropdownView())
        _view_registered = True
    print(f"✅ Bot conectado como {bot.user} (ID: {bot.user.id})")
    print("✅ Comandos slash sincronizados.")


@bot.tree.command(name="admin", description="Painel exclusivo do administrador")
async def admin_panel(interaction: discord.Interaction) -> None:
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "❌ Acesso negado. Comando restrito ao administrador.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="⚙️ Painel de Controle Admin",
        description=(
            "**Cadastrar** — novo produto no banco.\n"
            "**Editar** — escolha no menu e altere nome/conteúdo.\n"
            "**Excluir** — remove do banco (ação imediata).\n"
            "**Listar** — vê todos os IDs e datas.\n\n"
            "_Após alterações, use `/enviar_painel` no canal público para atualizar o menu fixo._"
        ),
        color=discord.Color.dark_grey(),
    )
    embed.set_footer(text="DOUGOBRASIL • Enterprise Control")
    await interaction.response.send_message(
        embed=embed,
        view=AdminControlView(),
        ephemeral=True,
    )


@bot.tree.command(name="enviar_painel", description="Envia e fixa o painel de resgate no canal atual")
async def enviar_painel(interaction: discord.Interaction) -> None:
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "❌ Acesso negado. Comando restrito ao administrador.",
            ephemeral=True,
        )
        return

    target_channel = interaction.channel
    if target_channel is None:
        await interaction.response.send_message(
            "❌ Não foi possível identificar o canal para publicar o painel.",
            ephemeral=True,
        )
        return

    if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message(
            "❌ Selecione um canal de texto (ou thread) para publicar o painel.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    try:
        panel_message = await target_channel.send(
            embed=build_painel_embed(),
            view=PersistentDropdownView(),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Não consigo **enviar mensagens** neste canal. "
            "Confira se o bot está no servidor e se tem permissão **Enviar mensagens** e **Incorporar links** "
            "(e nas permissões do canal, se algo estiver bloqueado para o cargo do bot).",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "❌ Falha ao publicar o painel. Tente novamente.",
            ephemeral=True,
        )
        return

    pin_ok = False
    try:
        await panel_message.pin(reason="Painel fixo do repositório de arquivos")
        pin_ok = True
    except discord.Forbidden:
        pass
    except discord.HTTPException:
        pass

    if pin_ok:
        await interaction.followup.send(
            f"✅ Painel enviado e fixado em {target_channel.mention}.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"✅ Painel enviado em {target_channel.mention}.\n"
            "⚠️ Não consegui **fixar** a mensagem: o bot precisa da permissão **Gerenciar mensagens** "
            "neste canal (ou fixe manualmente).",
            ephemeral=True,
        )


def main() -> None:
    # Inicia o Flask em uma thread daemon para não bloquear o bot
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("🌐 Servidor Flask iniciado na porta 8080.")

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
