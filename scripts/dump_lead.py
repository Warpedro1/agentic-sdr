"""Mostra tudo o que a Marina gravou sobre um lead.

    python scripts/dump_lead.py --telefone "+5511987654321"
    python scripts/dump_lead.py --telefone "+5511987654321" --empresa acme

E a ferramenta de conferencia: cadastro, documento, agendamento com o consultor
atribuido e a transcricao inteira da conversa — entrada e saida, com o template
que produziu cada resposta.

**O mesmo telefone pode ter dois cadastros.** Cada instalacao do juridico e um
escritorio diferente, e a mesma pessoa atendida por dois deles tem historicos
separados. Sem `--empresa` este script mostra TODOS, cada um sob o nome da sua
empresa: quem investiga quer ver o que a Marina guardou sobre aquele numero, e
esconder metade atras de um argumento so faria rodar o comando duas vezes.
"""

from __future__ import annotations

import argparse

from sqlalchemy import select

from app.aio import executar, forcar_utf8
from app.config import obter_configuracao
from app.datas import data_extenso, hora
from app.db import encerrar, sessao
from app.empresas import EmpresaDesconhecida, resolver_argumento
from app.models import (
    Agendamento,
    Consultor,
    Conversa,
    Documento,
    Empresa,
    Lead,
    Mensagem,
    OptOut,
)


def _linha(titulo: str) -> None:
    print(f"\n{titulo}\n{'-' * len(titulo)}")


async def _despejar(s, lead: Lead, fuso: str) -> None:
    """Tudo sobre UM cadastro."""
    _linha("LEAD")
    print(f"nome            : {lead.nome}")
    print(f"telefone        : {lead.telefone_e164}")
    print(f"e-mail          : {lead.email}")
    print(f"fonte           : {lead.fonte}  (disse: {lead.fonte_raw!r})")
    print(f"tipo de contrato: {lead.tipo_contrato}  (disse: {lead.tipo_contrato_raw!r})")
    print(f"tem o contrato  : {lead.possui_contrato}")
    print(f"consentimento   : {lead.consentimento_em}")
    print(f"canal           : {lead.canal}")
    print(f"id no CRM       : {lead.crm_lead_id or '(nenhum)'}")

    _linha("DOCUMENTOS")
    documentos = (
        (await s.execute(select(Documento).where(Documento.lead_id == lead.id)))
        .scalars()
        .all()
    )
    if not documentos:
        print("(nenhum)")
    for documento in documentos:
        print(
            f"{documento.caminho}  {documento.mime}  "
            f"{documento.tamanho_bytes} bytes  em {documento.recebido_em}"
        )

    _linha("AGENDAMENTOS")
    linhas = (
        await s.execute(
            select(Agendamento, Consultor)
            .join(Consultor, Consultor.id == Agendamento.consultor_id)
            .where(Agendamento.lead_id == lead.id)
            .order_by(Agendamento.inicio_utc)
        )
    ).all()
    if not linhas:
        print("(nenhum)")
    for agendamento, consultor in linhas:
        # O aviso existe porque isto era possivel antes do escopo por empresa, e
        # o historico guarda os casos.
        alheio = "  ! consultor de OUTRA empresa" if consultor.empresa_id != lead.empresa_id else ""
        print(
            f"{data_extenso(agendamento.inicio_utc, fuso)} às "
            f"{hora(agendamento.inicio_utc, fuso)} com {consultor.nome} "
            f"[{agendamento.status}]{alheio}"
        )

    _linha("OPT-OUT")
    # Escopado: descadastrar numa empresa nao suprime nas outras, entao mostrar o
    # registro alheio levaria a conclusao errada.
    criterio = [OptOut.empresa_id == lead.empresa_id]
    if lead.telefone_e164:
        criterio.append(OptOut.telefone_e164 == lead.telefone_e164)
    else:
        criterio.append(OptOut.email == lead.email)
    registros = (await s.execute(select(OptOut).where(*criterio))).scalars().all()
    if not registros:
        print("(nenhum)")
    for registro in registros:
        estado = "revogado" if registro.revogado_em else "ATIVO"
        print(f"{registro.criado_em}  {registro.motivo}  [{estado}]")

    conversas = (
        (
            await s.execute(
                select(Conversa)
                .where(Conversa.lead_id == lead.id)
                .order_by(Conversa.criada_em)
            )
        )
        .scalars()
        .all()
    )

    for indice, conversa in enumerate(conversas, start=1):
        _linha(
            f"CONVERSA {indice}/{len(conversas)} — {conversa.estado} ({conversa.canal})"
        )
        mensagens = (
            (
                await s.execute(
                    select(Mensagem)
                    .where(Mensagem.conversa_id == conversa.id)
                    .order_by(Mensagem.sequencia)
                )
            )
            .scalars()
            .all()
        )
        for mensagem in mensagens:
            seta = "<<" if mensagem.direcao == "in" else ">>"
            rotulo = f"[{mensagem.template_key}]" if mensagem.template_key else ""
            texto = mensagem.conteudo.replace("\n", "\n     ")
            print(f"{seta} {rotulo}")
            print(f"     {texto}")


async def main(telefone: str, slug: str | None) -> int:
    forcar_utf8()
    config = obter_configuracao()
    fuso = config.timezone_exibicao

    async with sessao() as s:
        criterio = [Lead.telefone_e164 == telefone]
        if slug:
            try:
                empresa = await resolver_argumento(s, slug)
            except EmpresaDesconhecida as erro:
                print(erro)
                return 2
            criterio.append(Lead.empresa_id == empresa.id)

        leads = (
            (await s.execute(select(Lead).where(*criterio).order_by(Lead.criado_em)))
            .scalars()
            .all()
        )
        if not leads:
            print(f"Nenhum lead com o telefone {telefone}.")
            return 1

        nomes = {
            linha.id: linha.slug for linha in await s.scalars(select(Empresa))
        }
        for lead in leads:
            if len(leads) > 1:
                dono = nomes.get(lead.empresa_id, "(sem empresa)")
                print(f"\n{'=' * 64}\nEMPRESA: {dono}\n{'=' * 64}")
            await _despejar(s, lead, fuso)

        if len(leads) > 1:
            print(f"\n{len(leads)} cadastros deste telefone. Filtre com --empresa <slug>.")

    await encerrar()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mostra o que a Marina gravou.")
    parser.add_argument("--telefone", required=True, help="ex.: +5511987654321")
    parser.add_argument("--empresa", default=None, help="slug, para filtrar")
    argumentos = parser.parse_args()
    raise SystemExit(executar(main(argumentos.telefone, argumentos.empresa)))
