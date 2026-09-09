"""Configuracao da Marina, lida do ambiente ou do arquivo .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Configuracao(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "postgresql+psycopg://marina:marina@localhost:5432/marina"

    # Agendamento
    antecedencia_minima_horas: int = 2
    horizonte_dias: int = 7
    slots_ofertados: int = 5

    # Conversa
    max_tentativas: int = 3
    janela_retomada_horas: int = 24

    # Documentos
    storage_dir: str = "storage"
    #: 16 MiB e o teto da Twilio por mensagem. Aceitar mais aqui so adiaria a
    #: recusa para um lugar onde a Marina nao tem como avisar o lead.
    doc_max_bytes: int = 16 * 1024 * 1024
    doc_extensoes: str = "pdf,jpg,jpeg,png,doc,docx"

    # WhatsApp via Twilio
    #: Do Console da Twilio. O auth token e o segredo que assina os webhooks —
    #: sem ele, qualquer um que descubra o endereco cria lead e reuniao falsos.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    #: Numero remetente em E.164, com ou sem o prefixo `whatsapp:`. No sandbox e o
    #: numero compartilhado da Twilio; em producao, o do escritorio.
    twilio_whatsapp_from: str = ""
    #: O endereco publico EXATO configurado no Console (sem barra no fim). A
    #: assinatura e calculada sobre a URL completa: se este valor divergir do que
    #: a Twilio chamou, toda chamada e rejeitada.
    twilio_url_publica: str = "http://localhost:8000"
    #: So o teste desliga. Em producao, desligar abre a porta para qualquer um.
    twilio_validar_assinatura: bool = True
    whatsapp_host: str = "127.0.0.1"
    whatsapp_porta: int = 8000

    # CRM externo (hoje o `juridico`)
    #: Desligado por padrao: sem isto a Marina se comporta exatamente como antes
    #: da integracao existir. Ligar e mudar uma linha.
    #: Chave GERAL: `false` desliga a integracao de todas as empresas de uma vez.
    #: E o desligamento de emergencia, nao o interruptor do dia a dia — quem liga e
    #: desliga uma empresa e o cadastro dela (`scripts/cadastrar_empresa.py`).
    #:
    #: O endereco e a credencial NAO moram mais aqui: cada instalacao do juridico e
    #: uma empresa, e sao dela. Uma credencial global seria a porta dos fundos para
    #: entregar o dado de um escritorio com a credencial de outro.
    juridico_ativo: bool = False
    juridico_timeout_s: float = 15.0
    #: Canal por onde a conversa chegou. Vai escrito na observacao registrada no
    #: CRM, ja que o cadastro de la nao tem campo proprio para isso.
    juridico_canal_contato: str = "WhatsApp"
    outbox_max_tentativas: int = 5

    # Apresentacao
    timezone_exibicao: str = "America/Sao_Paulo"
    log_level: str = "INFO"
    #: Arquivo de log. Vazio = escreve no terminal, o que no CLI mistura JSON no
    #: meio da conversa e deixa o atendimento ilegivel.
    log_arquivo: str = "marina.log"

    @property
    def twilio_remetente(self) -> str:
        """O remetente no formato que a API quer: `whatsapp:+55...`."""
        numero = self.twilio_whatsapp_from.strip()
        if not numero:
            return ""
        return numero if numero.startswith("whatsapp:") else f"whatsapp:{numero}"

    @property
    def extensoes_permitidas(self) -> frozenset[str]:
        return frozenset(
            item.strip().lower().lstrip(".")
            for item in self.doc_extensoes.split(",")
            if item.strip()
        )


@lru_cache
def obter_configuracao() -> Configuracao:
    return Configuracao()
