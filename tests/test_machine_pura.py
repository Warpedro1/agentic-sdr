"""A maquina precisa continuar pura. Este teste e a cerca.

Sem ele, daqui a alguns meses alguem "so resolve rapidinho" com um SELECT dentro
de machine.py — e no dia seguinte tests/test_machine.py precisa de Postgres. E
assim que esse tipo de arquitetura morre: nao de uma vez, mas por um atalho.
"""

import ast
import subprocess
import sys
from pathlib import Path

CAMINHO_MAQUINA = Path(__file__).resolve().parents[1] / "app" / "machine.py"

#: Bibliotecas que denunciam I/O escondido dentro da funcao pura.
#:
#: Aqui a lista e de PROIBIDOS, e esta certo: biblioteca de terceiros e biblioteca
#: padrao nao nascem dentro deste repositorio, entao enumerar nao envelhece.
IMPORTS_PROIBIDOS = {
    "sqlalchemy",
    "psycopg",
    "asyncio",
    "os",
    "pathlib",
    "zoneinfo",
    "socket",
    "requests",
    "httpx",
    "random",
    "fastapi",
    "uvicorn",
    "twilio",
    "yaml",
}

#: Modulos DESTE projeto que a maquina pode importar. Aqui a lista e de
#: AUTORIZADOS, e a inversao e o ponto.
#:
#: Enumerar o proibido funcionava enquanto o projeto era pequeno, e falhou em
#: silencio assim que cresceu: `app/segredos.py` nasceu depois da lista, le
#: ambiente e disco, e a maquina podia importa-lo sem nenhum teste reclamar. O
#: mesmo valia para os modulos de integracao, os de web e o adaptador de WhatsApp.
#:
#: Com a lista invertida, modulo novo e recusado por padrao — inclusive o que
#: ainda nao existe. Ampliar a camada pura passa a ser uma decisao explicita, que
#: se toma editando esta linha e justificando no commit.
IMPORTS_INTERNOS_AUTORIZADOS = {
    "app.channels.base",  # o contrato de mensagem (sem I/O, por isso pode)
    "app.enums",
    "app.guards",
    "app.parsers",
    "app.templates",
}

#: Chamadas que quebram o determinismo dos testes. O horario atual chega em
#: ctx["agora"]; o catalogo e injetado; nada e sorteado.
CHAMADAS_PROIBIDAS = {
    ("datetime", "now"),
    ("datetime", "utcnow"),
    ("date", "today"),
    ("time", "time"),
    ("uuid", "uuid4"),
}

FUNCOES_PROIBIDAS = {"open", "input", "print"}


def _arvore() -> ast.Module:
    return ast.parse(CAMINHO_MAQUINA.read_text(encoding="utf-8"))


def _modulos_importados() -> set[str]:
    modulos: set[str] = set()
    for no in ast.walk(_arvore()):
        if isinstance(no, ast.Import):
            modulos.update(alias.name for alias in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module:
            modulos.add(no.module)
    return modulos


def test_machine_nao_importa_io():
    importados = _modulos_importados()
    raizes = {modulo.split(".")[0] for modulo in importados} | importados
    proibidos_encontrados = raizes & IMPORTS_PROIBIDOS
    assert not proibidos_encontrados, (
        f"app/machine.py importou {sorted(proibidos_encontrados)}. "
        "A maquina e pura: peca o dado ao engine via ctx ou via um Efeito."
    )


def test_machine_so_importa_o_que_foi_autorizado_do_projeto():
    """A cerca por construcao: modulo interno novo e recusado por padrao.

    Este teste existe porque o anterior nao basta. Ele so pega o que alguem
    lembrou de proibir — e um modulo criado depois da lista passa direto, mesmo
    lendo disco e ambiente. Aqui a pergunta e a inversa: este import esta na lista
    do que a camada pura pode ver?
    """
    internos = {
        modulo
        for modulo in _modulos_importados()
        if modulo == "app" or modulo.startswith("app.")
    }
    nao_autorizados = internos - IMPORTS_INTERNOS_AUTORIZADOS
    assert not nao_autorizados, (
        f"app/machine.py importou {sorted(nao_autorizados)}, que nao esta na "
        "camada pura. Ou o dado chega pelo ctx (quem preenche e o engine), ou a "
        "maquina emite um Efeito e o engine executa. Se o modulo for mesmo puro, "
        "acrescente-o a IMPORTS_INTERNOS_AUTORIZADOS e diga no commit por que."
    )


def test_machine_nao_le_o_relogio_nem_sorteia():
    encontradas = set()
    for no in ast.walk(_arvore()):
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Attribute):
            alvo = no.func.value
            if isinstance(alvo, ast.Name):
                encontradas.add((alvo.id, no.func.attr))
    assert not (encontradas & CHAMADAS_PROIBIDAS), (
        "app/machine.py leu o relogio ou sorteou algo. Use ctx['agora'] — senao "
        "os testes de transicao param de ser deterministicos."
    )


def test_machine_nao_abre_arquivo_nem_fala_com_o_terminal():
    encontradas = {
        no.func.id
        for no in ast.walk(_arvore())
        if isinstance(no, ast.Call) and isinstance(no.func, ast.Name)
    }
    assert not (encontradas & FUNCOES_PROIBIDAS), (
        "app/machine.py chamou open/input/print. Quem fala com o mundo e o "
        "adaptador de canal; quem persiste e o engine."
    )


#: Pilhas que so aparecem por tabela: ninguem escreve `import sqlalchemy` na
#: maquina, mas basta importar um modulo que o faca. Banco, rede e web sao os tres
#: caminhos por onde I/O entra sem aparecer como import direto.
STACKS_PROIBIDAS = ("sqlalchemy", "psycopg", "alembic", "httpx", "fastapi", "uvicorn")


def test_importar_a_maquina_nao_carrega_stack_de_io():
    # A checagem de AST pega o import direto. Esta pega o INDIRETO: se qualquer
    # modulo na cadeia de app.machine puxar banco, rede ou web, aparece aqui.
    resultado = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.machine, sys; "
            f"proibidos = [m for m in {STACKS_PROIBIDAS!r} if m in sys.modules]; "
            "print(','.join(proibidos))",
        ],
        capture_output=True,
        text=True,
        cwd=CAMINHO_MAQUINA.parents[1],
    )
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "", (
        f"importar app.machine carregou {resultado.stdout.strip()} por tabela. "
        "Alguem na cadeia de imports puxou banco, rede ou web."
    )
