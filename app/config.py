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
    doc_max_bytes: int = 20 * 1024 * 1024
    doc_extensoes: str = "pdf,jpg,jpeg,png,doc,docx"

    # Apresentacao
    timezone_exibicao: str = "America/Sao_Paulo"
    log_level: str = "INFO"
    #: Arquivo de log. Vazio = escreve no terminal, o que no CLI mistura JSON no
    #: meio da conversa e deixa o atendimento ilegivel.
    log_arquivo: str = "marina.log"

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
