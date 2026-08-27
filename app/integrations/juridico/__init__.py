"""Integracao com o backend `juridico`.

TUDO o que e especifico do juridico mora aqui dentro. A maquina de estados, o
engine e o modelo de dados central nao sabem que este pacote existe — eles
conhecem so a interface `CrmSink`.

Essa fronteira nao e enfeite: a Marina vai atender mais de um sistema, e o
segundo backend entra como um pacote irmao, sem tocar em nada do fluxo.
"""
