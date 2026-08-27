"""Mostra tudo o que a Marina gravou sobre um lead.

    python scripts/dump_lead.py --telefone "+5511987654321"

E a ferramenta de conferencia do MVP: cadastro, documento, agendamento com o
consultor atribuido e a transcricao inteira da conversa — entrada e saida, com o
template que produziu cada resposta.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import select

from app.aio import executar
from app.config import obter_configuracao
from app.datas import data_extenso, hora
from app.db import encerrar, sessao
from app.models import Agendamento, Consultor, Conversa, Documento, Lead, Mensagem, OptOut


def _forcar_utf8() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _linha(titulo: str) -> None:
    print(f"\n{titulo}\n{'-' * len(titulo)}")


async def main(telefone: str) -> int:
    _forcar_utf8()
    config = obter_configuracao()
    fuso = config.timezone_exibicao

    async with sessao() as s:
        lead = (
            await s.execute(select(Lead).where(Lead.telefone_e164 == telefone))
        ).scalar_one_or_none()

        if lead is None:
            print(f"Nenhum lead com o telefone {telefone}.")
            return 1

        _linha("LEAD")
        print(f"nome            : {lead.nome}")
        print(f"telefone        : {lead.telefone_e164}")
        print(f"e-mail          : {lead.email}")
        print(f"fonte           : {lead.fonte}  (disse: {lead.fonte_raw!r})")
        print(
            f"tipo de contrato: {lead.tipo_contrato}  (disse: {lead.tipo_contrato_raw!r})"
        )
        print(f"tem o contrato  : {lead.possui_contrato}")
        print(f"consentimento   : {lead.consentimento_em}")
        print(f"canal           : {lead.canal}")

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
            print(
                f"{data_extenso(agendamento.inicio_utc, fuso)} às "
                f"{hora(agendamento.inicio_utc, fuso)} com {consultor.nome} "
                f"[{agendamento.status}]"
            )

        _linha("OPT-OUT")
        registros = (
            (await s.execute(select(OptOut).where(OptOut.telefone_e164 == telefone)))
            .scalars()
            .all()
        )
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
                f"CONVERSA {indice}/{len(conversas)} — {conversa.estado} "
                f"({conversa.canal})"
            )
            mensagens = (
                (
                    await s.execute(
                        select(Mensagem)
                        .where(Mensagem.conversa_id == conversa.id)
                        .order_by(Mensagem.criada_em, Mensagem.id)
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

    await encerrar()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mostra o que a Marina gravou.")
    parser.add_argument("--telefone", required=True, help="ex.: +5511987654321")
    argumentos = parser.parse_args()
    raise SystemExit(executar(main(argumentos.telefone)))
