"""Capacidade e agendamento.

Quatro modulos que so conversam entre si, com o modelo e os enums. Viraram pacote
pelo mesmo criterio que fez `channels/` e `storage/` virarem: e aqui que se espera
uma segunda implementacao — `CalendarProvider` existe justamente para o Google
Calendar entrar sem tocar na maquina de estados.

A divisao de trabalho entre eles:

- `disponibilidade` — quais horarios existem, expandindo as regras semanais
- `carga` — quem esta mais livre, para decidir se o horario cabe
- `calendario` — o que ja esta ocupado (hoje a tabela; amanha um calendario externo)
- `reserva` — a gravacao em si, com as duas defesas contra dupla marcacao

**A reuniao nasce sem dono.** O consultor e ficha de capacidade, nao atribuicao: a
copy nunca promete um nome, e no CRM a tarefa fica em aberto.

Tudo aqui e escopado por empresa. Cada instalacao do juridico e um escritorio
diferente, e oferecer o horario de um profissional de outro consumiria a
capacidade dele.
"""

from app.agenda.calendario import CalendarProvider, Ocupacao, PostgresCalendarProvider
from app.agenda.carga import carga_futura, escolher_consultor, ordenar_por_carga
from app.agenda.disponibilidade import (
    Candidato,
    Oferta,
    expandir_regra,
    gerar_candidatos,
    montar_ofertas,
)
from app.agenda.reserva import ResultadoReserva, reservar

__all__ = [
    "CalendarProvider",
    "Candidato",
    "Ocupacao",
    "Oferta",
    "PostgresCalendarProvider",
    "ResultadoReserva",
    "carga_futura",
    "escolher_consultor",
    "expandir_regra",
    "gerar_candidatos",
    "montar_ofertas",
    "ordenar_por_carga",
    "reservar",
]
