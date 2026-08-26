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

#: Modulos que denunciam I/O escondido dentro da funcao pura.
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
    "app.db",
    "app.models",
    "app.config",
    "app.engine",
    "app.availability",
    "app.routing",
    "app.booking",
    "app.calendar",
    "app.crm",
    "app.storage",
    "app.channels.terminal",
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


def test_importar_a_maquina_nao_carrega_o_stack_de_banco():
    # A checagem de AST pega o import direto. Esta pega o indireto: se qualquer
    # modulo na cadeia de app.machine puxar SQLAlchemy, aparece aqui.
    resultado = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.machine, sys; "
            "proibidos = [m for m in ('sqlalchemy', 'psycopg', 'alembic') "
            "if m in sys.modules]; "
            "print(','.join(proibidos))",
        ],
        capture_output=True,
        text=True,
        cwd=CAMINHO_MAQUINA.parents[1],
    )
    assert resultado.returncode == 0, resultado.stderr
    assert resultado.stdout.strip() == "", (
        f"importar app.machine carregou {resultado.stdout.strip()} por tabela. "
        "Alguem na cadeia de imports puxou o banco."
    )
