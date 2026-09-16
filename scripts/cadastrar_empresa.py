"""Cadastra uma empresa atendida pela Marina.

    python scripts/cadastrar_empresa.py --slug acme \
        --whatsapp "+5511999990000" --base-url "https://acme.juridico.com.br"
    python scripts/cadastrar_empresa.py --listar
    python scripts/cadastrar_empresa.py --slug acme --atualizar --whatsapp "+5511888880000"

Cada instalação do juridico é uma empresa. Cadastrar significa ligar três coisas: o
número de WhatsApp por onde a mensagem chega, o endereço do juridico para onde o
lead vai, e a credencial que abre aquele juridico.

**A credencial não entra no banco.** Ela vive no `.env`, numa variável derivada do
slug (`JURIDICO_TOKEN_ACME`), e o banco guarda só o nome dela. Gere o token no
juridico daquela empresa com `php artisan integracao:usuario`.

O cadastro **valida a credencial na hora**, contra o juridico informado. Uma empresa
cadastrada com token inválido não falha no dia do cadastro: ela acumula uma fila de
entrega morta durante uma semana até alguém reparar.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.aio import executar, forcar_utf8
from app.db import encerrar, obter_engine
from app.empresas import PREFIXO_TOKEN_ZAPI, slug_para_env
from app.integrations.juridico.cliente import ClienteJuridico, ErroJuridico
from app.logging import configurar_logging
from app.models import Consultor, CrmOutbox, Empresa, RegraDisponibilidade
from app.segredos import ler as ler_segredo

PERMISSOES_NECESSARIAS = ("comercial.negociacoes", "comercial.tarefas")


def montar_argumentos() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", help="chave estável da empresa (ex.: acme)")
    parser.add_argument(
        "--nome", help="razão social; padrão: o nome que o juridico devolve"
    )
    parser.add_argument("--nome-curto", help="como citá-la na frase (ex.: Acme)")
    parser.add_argument("--whatsapp", help="número em E.164 que recebe as mensagens")
    parser.add_argument(
        "--whatsapp-id",
        help="identificador do número na Meta (phone_number_id), para o caminho da Cloud API",
    )
    parser.add_argument(
        "--zapi-instancia",
        help="id da instância na Z-API; o token dela vai no .env como ZAPI_TOKEN_<SLUG>",
    )
    parser.add_argument("--base-url", help="endereço do juridico dessa empresa")
    parser.add_argument(
        "--sem-juridico",
        action="store_true",
        help="cadastra sem validar credencial (a empresa nasce sem integração)",
    )
    parser.add_argument(
        "--atualizar", action="store_true", help="altera uma empresa já cadastrada"
    )
    parser.add_argument(
        "--desativar", action="store_true", help="para de atender a empresa"
    )
    parser.add_argument("--listar", action="store_true", help="diagnóstico de todas")
    return parser.parse_args()


async def _diagnostico(sessao) -> int:
    """Responde "por que a empresa X não está recebendo nada" sem abrir o banco."""
    empresas = list(await sessao.scalars(select(Empresa).order_by(Empresa.slug)))
    if not empresas:
        print("Nenhuma empresa cadastrada.")
        return 1

    for empresa in empresas:
        token = ler_segredo(empresa.juridico_token_env)
        ativos = await sessao.scalar(
            select(func.count(Consultor.id)).where(
                Consultor.empresa_id == empresa.id, Consultor.ativo.is_(True)
            )
        )
        com_agenda = await sessao.scalar(
            select(func.count(func.distinct(RegraDisponibilidade.consultor_id))).where(
                RegraDisponibilidade.empresa_id == empresa.id
            )
        )
        pendentes = await sessao.scalar(
            select(func.count(CrmOutbox.id)).where(
                CrmOutbox.empresa_id == empresa.id, CrmOutbox.entregue_em.is_(None)
            )
        )

        marca = "" if empresa.ativo else "  (DESATIVADA)"
        print(f"\n{empresa.slug}{marca}")
        print(f"  nome          {empresa.nome}")
        print(f"  citada como   {empresa.nome_curto or empresa.nome}")
        print(f"  whatsapp      {empresa.whatsapp_numero or '(sem número — não recebe)'}")
        print(f"  meta          {empresa.whatsapp_numero_id or '(sem id — não recebe pela Meta)'}")
        print(f"  zapi          {empresa.zapi_instancia_id or '(sem instância — não recebe pela Z-API)'}")
        print(f"  juridico      {empresa.juridico_base_url or '(sem endereço)'}")
        estado = "ligada" if empresa.juridico_ativo else "desligada"
        # Nunca o valor da credencial: só se ela está presente.
        presenca = "presente" if token else f"AUSENTE ({empresa.juridico_token_env})"
        print(f"  integração    {estado}, credencial {presenca}")
        print(f"  consultores   {ativos or 0} ativos, {com_agenda or 0} com agenda")
        print(f"  fila          {pendentes or 0} por entregar")

        if empresa.ativo and not (
            empresa.whatsapp_numero or empresa.whatsapp_numero_id or empresa.zapi_instancia_id
        ):
            print("  ! sem número, id da Meta nem instância da Z-API: nenhuma mensagem será atribuída a ela")
        variavel_zapi = slug_para_env(empresa.slug, PREFIXO_TOKEN_ZAPI)
        if empresa.zapi_instancia_id and not ler_segredo(variavel_zapi):
            # Nunca o valor: só se ele está presente.
            print(f"  ! token da instância AUSENTE ({variavel_zapi}): a Marina recebe e não responde")
        if empresa.juridico_ativo and not token:
            print("  ! credencial ausente: a integração fica desligada em silêncio")
        if (ativos or 0) and not (com_agenda or 0):
            print("  ! nenhum consultor com agenda: não há horário para oferecer")
    print()
    return 0


async def _validar(base_url: str, token: str) -> dict | None:
    """Confere a credencial contra o juridico e devolve quem ele diz que somos."""
    cliente = ClienteJuridico(base_url=base_url, token=token)
    try:
        return await cliente.contexto()
    except ErroJuridico as erro:
        print(f"O juridico em {base_url} recusou a credencial: {erro}")
        return None
    finally:
        await cliente.fechar()


async def _desativar(sessao, slug: str) -> int:
    empresa = await sessao.scalar(select(Empresa).where(Empresa.slug == slug))
    if empresa is None:
        print(f"Empresa '{slug}' não encontrada.")
        return 1
    empresa.ativo = False
    await sessao.commit()
    print(f"Empresa '{slug}' desativada. As mensagens para o número dela deixam de")
    print("ser atendidas — o histórico continua onde está.")
    return 0


async def _gravar(sessao, args: argparse.Namespace, slug: str) -> int:
    existente = await sessao.scalar(select(Empresa).where(Empresa.slug == slug))

    if existente is not None and not args.atualizar:
        print(f"Empresa '{slug}' já existe. Use --atualizar para alterar,")
        print("ou --listar para ver como ela está.")
        return 1
    if existente is None and args.atualizar:
        print(f"Empresa '{slug}' não existe — não há o que atualizar.")
        return 1

    nome_env = slug_para_env(slug)
    base_url = (
        args.base_url or (existente.juridico_base_url if existente else "")
    ).strip()
    contexto: dict | None = None

    if not args.sem_juridico:
        token = ler_segredo(nome_env)
        if not token:
            print(f"Credencial ausente. Ponha no .env:\n\n  {nome_env}=<token>\n")
            print("Gere o token no juridico dessa empresa com:")
            print('  php artisan integracao:usuario "<organização>"')
            print("\nOu use --sem-juridico para cadastrar sem integração.")
            return 2
        if not base_url:
            print("Informe --base-url (o endereço do juridico dessa empresa).")
            return 2

        contexto = await _validar(base_url, token)
        if contexto is None:
            print("\nNada foi gravado — uma empresa com credencial inválida")
            print("acumularia fila morta em silêncio.")
            return 1

        permissoes = contexto.get("permissoes") or []
        faltando = [
            p
            for p in PERMISSOES_NECESSARIAS
            if p not in permissoes and "*" not in permissoes
        ]
        if faltando:
            print(f"! A credencial não tem: {', '.join(faltando)}.")
            print("  O cadastro segue, mas a entrega vai falhar.")

    remoto = (contexto or {}).get("empresa") or {}
    nome_remoto = (remoto.get("nome") or "").strip()
    nome_local = (existente.nome if existente else "").strip()

    # O nome do CRM e adotado no CADASTRO, para o lead ouvir a mesma denominacao
    # dos dois lados. Mas uma ATUALIZACAO nao pode renomear a empresa em silencio:
    # esse nome e o que a Marina fala na saudacao, e o juridico costuma devolver o
    # rotulo da organizacao, que nem sempre e a razao social.
    nome = (args.nome or nome_local or nome_remoto).strip()
    if not nome:
        print("Sem --nome e sem resposta do juridico, não há nome para a empresa.")
        return 2
    if nome_remoto and nome_remoto != nome:
        print(f'! O juridico se chama "{nome_remoto}"; aqui a empresa é "{nome}".')
        print("  A saudação usa o nome daqui. Use --nome para alinhar os dois.")

    curto = (
        args.nome_curto
        or (existente.nome_curto if existente else "")
        or nome.split()[0]
    ).strip()
    numero = (
        args.whatsapp or (existente.whatsapp_numero if existente else "") or ""
    ).strip()
    numero_id = (
        args.whatsapp_id or (existente.whatsapp_numero_id if existente else "") or ""
    ).strip()
    instancia_zapi = (
        args.zapi_instancia or (existente.zapi_instancia_id if existente else "") or ""
    ).strip()

    if existente is None:
        existente = Empresa(slug=slug)
        sessao.add(existente)
    existente.nome = nome
    existente.nome_curto = curto
    existente.ativo = True
    existente.whatsapp_numero = numero or None
    existente.whatsapp_numero_id = numero_id or None
    existente.zapi_instancia_id = instancia_zapi or None
    existente.juridico_base_url = base_url
    existente.juridico_token_env = nome_env
    existente.juridico_ativo = not args.sem_juridico
    if remoto.get("id"):
        existente.crm_empresa_id = str(remoto["id"])
    existente.atualizado_em = datetime.now(UTC)
    await sessao.commit()

    print(f'\nEmpresa "{slug}" pronta: {nome} (citada como "{curto}").')
    if not numero and not numero_id and not instancia_zapi:
        print("! Sem número, id da Meta nem instância da Z-API: nenhuma mensagem será atribuída a ela.")
    print("\nPróximos passos:")
    if numero:
        print(f"  1. No Console da Twilio, aponte {numero} para")
        print("     <TWILIO_URL_PUBLICA>/webhooks/twilio/whatsapp")
    if numero_id:
        print("  1. No app da Meta, aponte o webhook para")
        print("     <endereço público>/webhooks/meta/whatsapp  (assine o campo messages)")
    if instancia_zapi:
        print(f"  1. Ponha no .env: {slug_para_env(slug, PREFIXO_TOKEN_ZAPI)}=<token da instância>")
        print(f"     e rode: python scripts/configurar_webhook_zapi.py --empresa {slug} --url-publica <endereço público>")
    print(f"  2. python scripts/sync_consultores.py --empresa {slug}")
    print(f"  3. python scripts/configurar_agenda.py --empresa {slug}")
    print("  4. python scripts/cadastrar_empresa.py --listar\n")
    return 0


async def main() -> int:
    forcar_utf8()
    args = montar_argumentos()
    configurar_logging(destino="")  # script: log no terminal mesmo

    criar_sessao = async_sessionmaker(obter_engine(), expire_on_commit=False)
    try:
        async with criar_sessao() as sessao:
            if args.listar:
                return await _diagnostico(sessao)
            if not args.slug:
                print("Informe --slug, ou use --listar.")
                return 2

            slug = args.slug.strip().lower()
            if args.desativar:
                return await _desativar(sessao, slug)
            return await _gravar(sessao, args, slug)
    finally:
        await encerrar()


if __name__ == "__main__":
    sys.exit(executar(main()))
