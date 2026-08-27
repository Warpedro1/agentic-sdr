"""Arquivamento de contratos em disco.

A Marina RECEBE e ARQUIVA. Ela nunca abre o arquivo, nunca le o conteudo e nunca
extrai nada dele — a analise do contrato e humana, feita por um consultor. Este
modulo copia bytes e valida envelope (extensao, tipo e tamanho); nada mais.
"""

from __future__ import annotations

import mimetypes
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.channels.base import Anexo
from app.config import Configuracao, obter_configuracao


class DocumentoRecusado(Exception):
    """O anexo nao passou na validacao de envelope."""


@dataclass(frozen=True)
class DocumentoGuardado:
    caminho: str
    mime: str
    tamanho_bytes: int


class ArmazenamentoDocumento(Protocol):
    async def salvar(self, anexo: Anexo, lead_id: str) -> DocumentoGuardado: ...


class LocalDocumentStorage:
    def __init__(self, config: Configuracao | None = None) -> None:
        self._config = config or obter_configuracao()
        self._raiz = Path(self._config.storage_dir)

    def _validar(self, anexo: Anexo, origem: Path) -> None:
        extensao = origem.suffix.lower().lstrip(".")
        if extensao not in self._config.extensoes_permitidas:
            permitidas = ", ".join(sorted(self._config.extensoes_permitidas))
            raise DocumentoRecusado(
                f"extensao '{extensao}' nao aceita (aceitas: {permitidas})"
            )
        if anexo.tamanho_bytes > self._config.doc_max_bytes:
            limite_mb = self._config.doc_max_bytes / (1024 * 1024)
            raise DocumentoRecusado(f"arquivo acima do limite de {limite_mb:.0f} MB")
        if anexo.tamanho_bytes <= 0:
            raise DocumentoRecusado("arquivo vazio")

    async def salvar(self, anexo: Anexo, lead_id: str) -> DocumentoGuardado:
        origem = Path(anexo.origem)
        if not origem.is_file():
            raise DocumentoRecusado("arquivo nao encontrado")

        self._validar(anexo, origem)

        # Um diretorio por lead, e nome unico: dois envios do mesmo "contrato.pdf"
        # nao podem se sobrescrever.
        destino_dir = self._raiz / lead_id
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / f"{uuid.uuid4().hex}{origem.suffix.lower()}"

        shutil.copy2(origem, destino)

        mime = (
            anexo.mime
            or mimetypes.guess_type(str(origem))[0]
            or "application/octet-stream"
        )
        return DocumentoGuardado(
            caminho=str(destino),
            mime=mime,
            tamanho_bytes=destino.stat().st_size,
        )
