"""
score_pronto_para_comprar.py

Calcula um score de "pronto para comprar" por cliente, combinando:
  - CSAT (avaliacoes de satisfacao do Zendesk)
  - Tipo de ticket (peso de intencao de compra por tag/tipo do ticket)
  - Tempo de primeira resposta (mais rapido = mais engajado)

Feito pra rodar direto contra a API real do Zendesk (via Claude Code ou qualquer
Python 3), usando suas credenciais. O resultado sai como CSV, pronto para importar
no seu dashboard (Metabase, planilha, etc), e opcionalmente grava o score de volta
em um campo customizado do cliente no Zendesk, pra ele aparecer nativamente em
views e no proprio Zendesk.

ISSO E UM PROTOTIPO: os pesos abaixo (PESOS_POR_TAG, PESOS_POR_TYPE, PALAVRAS_CHAVE)
sao um ponto de partida. Ajuste as tags/palavras-chave para bater com a realidade
da sua conta e calibre os pesos com o time comercial antes de usar o score pra
decisao de negocio.

CONFIGURACAO
------------
Defina as variaveis de ambiente antes de rodar (Admin Center > Apps e integracoes
> APIs > Zendesk API, para gerar o token):

    export ZENDESK_SUBDOMAIN="suaempresa"        # https://suaempresa.zendesk.com
    export ZENDESK_EMAIL="voce@suaempresa.com"
    export ZENDESK_API_TOKEN="xxxxxxxxxxxxxxxx"

USO
---
    pip install requests

    python score_pronto_para_comprar.py --dias 30 --saida ranking.csv

    # Pular o calculo de tempo de resposta (1 chamada de API por ticket com CSAT;
    # em contas grandes pode ser lento / bater rate limit). O score sai so com
    # CSAT + tipo, com pesos redistribuidos automaticamente.
    python score_pronto_para_comprar.py --dias 30 --sem-tempo-resposta

    # Gravar o score no campo customizado do usuario no Zendesk (precisa existir:
    # Admin Center > Objetos e regras > Campos > Usuario > criar campo tipo numero,
    # e usar aqui a "key" do campo, nao o titulo).
    python score_pronto_para_comprar.py --dias 30 --gravar-campo score_pronto_comprar
"""

import argparse
import csv
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIGURACAO DE PESOS - ajuste livremente para o seu negocio
# ---------------------------------------------------------------------------

# Peso final = W_CSAT*csat_norm + W_TIPO*tipo_norm + W_TEMPO*tempo_norm (soma = 1.0)
W_CSAT = 0.4
W_TIPO = 0.3
W_TEMPO = 0.3

# 1) Primeiro tenta achar o peso por TAG do ticket (ajuste para as tags reais da sua conta)
PESOS_POR_TAG = {
    "duvida_uso": 75,
    "duvida_produto": 75,
    "elogio": 70,
    "cupom": 65,
    "assinatura": 80,
    "documentacao": 55,
    "rastreio": 45,
    "entrega": 45,
    "financeiro": 40,
    "reclamacao_atendimento": 30,
    "reclamacao_produto": 20,
    "cancelamento": 10,
    "optout": 5,
}

# 2) Se nao achar tag conhecida, tenta pelo campo "type" nativo do Zendesk
PESOS_POR_TYPE = {
    "question": 70,
    "task": 50,
    "incident": 35,
    "problem": 20,
}

# 3) Se nao achar nem tag nem type mapeado, tenta por palavra-chave no assunto/descricao
#    (mesma logica usada no relatorio de motivos de cancelamento: e uma inferencia,
#    nao um dado estruturado do Zendesk - trate como aproximacao)
PALAVRAS_CHAVE = {
    "cancelamento": ["cancelar", "cancelamento", "desistir", "desisti"],
    "reclamacao_produto": ["reclamacao", "reclamação", "nao funcionou", "não funcionou", "fake"],
    "reclamacao_atendimento": ["robo", "robô", "ia ", " ia", "nao resolveu", "não resolveu", "demorado"],
    "rastreio": ["rastreio", "rastrear", "entrega", "transportadora"],
    "documentacao": ["laudo", "anvisa", "documento", "autorizacao", "autorização"],
    "financeiro": ["desconto", "cupom", "financeira", "pagamento", "cartao", "cartão"],
    "elogio": ["obrigad", "excelente", "otimo", "ótimo", "parabens", "parabéns"],
}

PESO_TIPO_PADRAO = 50  # quando nada bate

# Tempo de primeira resposta (calendario, em minutos): <= RAPIDO -> 100, >= LENTO -> 0,
# interpolado linear entre os dois.
TEMPO_MIN_RAPIDO_MIN = 10
TEMPO_MAX_LENTO_MIN = 240


def _auth():
    subdomain = os.environ.get("ZENDESK_SUBDOMAIN")
    email = os.environ.get("ZENDESK_EMAIL")
    token = os.environ.get("ZENDESK_API_TOKEN")
    if not all([subdomain, email, token]):
        sys.exit(
            "Defina ZENDESK_SUBDOMAIN, ZENDESK_EMAIL e ZENDESK_API_TOKEN como variaveis de ambiente."
        )
    return subdomain, requests.auth.HTTPBasicAuth(f"{email}/token", token)


def _get(session, base_url, path, params=None):
    resp = session.get(f"{base_url}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def buscar_tickets_com_csat(session, base_url, start_time):
    """Exportacao incremental cursor-based de tickets. Cada ticket ja vem com
    satisfaction_rating, tags, type e requester_id embutidos - sem chamada extra."""
    tickets = []
    params = {"start_time": start_time}
    path = "/api/v2/incremental/tickets/cursor"
    while True:
        data = _get(session, base_url, path, params)
        tickets.extend(data.get("tickets", []))
        if data.get("end_of_stream"):
            break
        params = {"cursor": data["after_cursor"]}
    return tickets


def buscar_tempo_primeira_resposta(session, base_url, ticket_id):
    """Minutos (calendario) até a primeira resposta do agente. None se nao houver."""
    try:
        data = _get(session, base_url, f"/api/v2/tickets/{ticket_id}/metrics.json")
    except requests.HTTPError:
        return None
    metric = data.get("ticket_metric", {})
    reply = metric.get("reply_time_in_minutes") or {}
    return reply.get("calendar")


def peso_tipo(ticket):
    for tag in ticket.get("tags", []):
        if tag in PESOS_POR_TAG:
            return PESOS_POR_TAG[tag]
    tipo = ticket.get("type")
    if tipo in PESOS_POR_TYPE:
        return PESOS_POR_TYPE[tipo]
    texto = f"{ticket.get('subject') or ''} {ticket.get('description') or ''}".lower()
    for categoria, palavras in PALAVRAS_CHAVE.items():
        if any(p in texto for p in palavras):
            return PESOS_POR_TAG.get(categoria, PESO_TIPO_PADRAO)
    return PESO_TIPO_PADRAO


def normaliza_tempo(minutos):
    if minutos is None:
        return None
    if minutos <= TEMPO_MIN_RAPIDO_MIN:
        return 100.0
    if minutos >= TEMPO_MAX_LENTO_MIN:
        return 0.0
    faixa = TEMPO_MAX_LENTO_MIN - TEMPO_MIN_RAPIDO_MIN
    return round(100 * (1 - (minutos - TEMPO_MIN_RAPIDO_MIN) / faixa), 1)


def buscar_email_usuario(session, base_url, user_id, cache):
    if user_id in cache:
        return cache[user_id]
    try:
        data = _get(session, base_url, f"/api/v2/users/{user_id}.json")
        email = data["user"].get("email") or data["user"].get("name") or f"user_{user_id}"
    except requests.HTTPError:
        email = f"user_{user_id}"
    cache[user_id] = email
    return email


def gravar_score_no_usuario(session, base_url, user_id, campo_key, score):
    payload = {"user": {"user_fields": {campo_key: score}}}
    resp = session.put(f"{base_url}/api/v2/users/{user_id}.json", json=payload, timeout=30)
    resp.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description="Score de pronto para comprar (Zendesk)")
    parser.add_argument("--dias", type=int, default=30, help="Janela de dias a analisar (padrao: 30)")
    parser.add_argument("--saida", default="ranking_pronto_comprar.csv", help="Arquivo CSV de saida")
    parser.add_argument(
        "--gravar-campo",
        default=None,
        help="Key do campo customizado de usuario no Zendesk para gravar o score (opcional)",
    )
    parser.add_argument(
        "--sem-tempo-resposta",
        action="store_true",
        help="Pula o calculo de tempo de resposta (evita 1 chamada de API extra por ticket)",
    )
    args = parser.parse_args()

    subdomain, auth = _auth()
    base_url = f"https://{subdomain}.zendesk.com"
    session = requests.Session()
    session.auth = auth

    inicio = datetime.now(timezone.utc) - timedelta(days=args.dias)
    start_time = int(inicio.timestamp())

    print(f"Buscando tickets desde {inicio.date()} (exportacao incremental)...")
    tickets = buscar_tickets_com_csat(session, base_url, start_time)
    print(f"{len(tickets)} tickets no periodo.")

    # pesos efetivos: se pular tempo de resposta, redistribui o peso entre CSAT e tipo
    if args.sem_tempo_resposta:
        w_csat, w_tipo, w_tempo = W_CSAT / (W_CSAT + W_TIPO), W_TIPO / (W_CSAT + W_TIPO), 0
    else:
        w_csat, w_tipo, w_tempo = W_CSAT, W_TIPO, W_TEMPO

    email_cache = {}
    por_cliente = defaultdict(list)
    com_csat = 0

    for ticket in tickets:
        rating = ticket.get("satisfaction_rating") or {}
        score_txt = rating.get("score")
        if score_txt not in ("good", "bad", "offered"):
            continue  # sem avaliacao no periodo, fora do escopo deste score
        com_csat += 1

        score_csat = {"good": 100, "bad": 0, "offered": 50}[score_txt]
        p_tipo = peso_tipo(ticket)

        score_tempo = 50.0  # neutro por padrao
        if not args.sem_tempo_resposta:
            minutos = buscar_tempo_primeira_resposta(session, base_url, ticket["id"])
            normalizado = normaliza_tempo(minutos)
            if normalizado is not None:
                score_tempo = normalizado
            time.sleep(0.05)  # gentil com o rate limit da API

        score = round(w_csat * score_csat + w_tipo * p_tipo + w_tempo * score_tempo, 1)

        requester_id = ticket.get("requester_id")
        if requester_id is None:
            continue
        por_cliente[requester_id].append(score)

    print(f"{com_csat} tickets com avaliacao CSAT no periodo.")

    ranking = []
    for user_id, scores in por_cliente.items():
        email = buscar_email_usuario(session, base_url, user_id, email_cache)
        media = round(sum(scores) / len(scores), 1)
        ranking.append((user_id, email, len(scores), media))
    ranking.sort(key=lambda r: -r[3])

    with open(args.saida, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["user_id", "email", "qtd_tickets_avaliados", "score_pronto_comprar"])
        writer.writerows(ranking)

    print(f"Ranking salvo em {args.saida} ({len(ranking)} clientes).")

    if args.gravar_campo:
        print(f"Gravando score no campo customizado '{args.gravar_campo}' de cada usuario...")
        falhas = 0
        for user_id, email, _, media in ranking:
            try:
                gravar_score_no_usuario(session, base_url, user_id, args.gravar_campo, media)
            except requests.HTTPError as e:
                falhas += 1
                print(f"  falhou para {email}: {e}")
        print(f"Concluido. {falhas} falha(s).")


if __name__ == "__main__":
    main()
