import os
import re
import time
import asyncio
from collections import defaultdict
from datetime import datetime
from typing import Optional

import requests
from fastapi import FastAPI, Request

app = FastAPI()

# =========================
# CONFIG
# =========================
DIGISAC_TOKEN = os.getenv("DIGISAC_TOKEN", "").strip()
DIGISAC_BASE_URL = os.getenv("DIGISAC_BASE_URL", "").strip().rstrip("/")
BLING_ACCESS_TOKEN = os.getenv("BLING_ACCESS_TOKEN", "").strip()
BLING_BASE_URL = "https://api.bling.com.br/Api/v3"

TIMEOUT = 60
MAX_PAGINAS_CONTATOS = 220
MAX_PAGINAS_CONTAS_GERAL = 80
MAX_PAGINAS_CONTAS_POR_PEDIDO = 80

# Situações consideradas em aberto
SITUACOES_EM_ABERTO = {1, 3}

# Controle simples de estado por ticket
ESTADOS = {}
CACHE_CONTATOS = {}


# =========================
# HELPERS
# =========================
def so_numeros(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def texto_normalizado(valor: str) -> str:
    return (valor or "").strip().lower()


def agora_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def headers_bling():
    return {
        "Authorization": f"Bearer {BLING_ACCESS_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def headers_digisac():
    return {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json",
    }


def extrair_texto(payload: dict) -> str:
    return (payload.get("data") or {}).get("text") or ""


def extrair_ticket_id(payload: dict) -> Optional[str]:
    return (payload.get("data") or {}).get("ticketId")


def extrair_contact_id(payload: dict) -> Optional[str]:
    return (payload.get("data") or {}).get("contactId")


def eh_mensagem_cliente(payload: dict) -> bool:
    data = payload.get("data") or {}
    return (
        payload.get("event") == "message.created"
        and not data.get("isFromMe", False)
        and data.get("type") == "chat"
        and bool((data.get("text") or "").strip())
    )


def obter_estado(ticket_id: str) -> dict:
    if ticket_id not in ESTADOS:
        ESTADOS[ticket_id] = {
            "etapa": "idle",
            "cpf_cnpj": None,
            "contato_bling": None,
            "modo_consulta": None,
            "pedido": None,
            "boletos": [],
        }
    return ESTADOS[ticket_id]


def limpar_estado(ticket_id: str):
    ESTADOS[ticket_id] = {
        "etapa": "idle",
        "cpf_cnpj": None,
        "contato_bling": None,
        "modo_consulta": None,
        "pedido": None,
        "boletos": [],
    }


# =========================
# DIGISAC
# =========================
def enviar_mensagem(contact_id: str, texto: str):
    url = f"{DIGISAC_BASE_URL}/messages"
    body = {
        "contactId": contact_id,
        "type": "chat",
        "text": texto
    }
    resp = requests.post(url, json=body, headers=headers_digisac(), timeout=30)
    print("Digisac mensagem:", resp.status_code, resp.text)
    return resp


def enviar_documento(contact_id: str, url_pdf: str):
    # Correção: envia como texto com link para evitar "undefined"
    texto = f"Segue seu boleto:\n{url_pdf}"
    return enviar_mensagem(contact_id, texto)


# =========================
# BLING BASE
# =========================
def request_bling(method: str, endpoint: str, params=None, max_retries=4):
    url = f"{BLING_BASE_URL}{endpoint}"
    tentativa = 0

    while tentativa < max_retries:
        tentativa += 1
        try:
            resp = requests.request(
                method,
                url,
                headers=headers_bling(),
                params=params,
                timeout=TIMEOUT,
            )

            if resp.status_code == 429:
                espera = min(2 * tentativa, 8)
                print(f"Rate limit Bling. Aguardando {espera}s...")
                time.sleep(espera)
                continue

            if resp.status_code in (500, 502, 503, 504):
                espera = min(2 * tentativa, 8)
                print(f"Bling {resp.status_code}. Retry em {espera}s...")
                time.sleep(espera)
                continue

            return resp

        except requests.RequestException as e:
            espera = min(2 * tentativa, 8)
            print(f"Erro de conexão Bling: {e}. Retry em {espera}s...")
            time.sleep(espera)

    return None


# =========================
# CONTATOS BLING
# =========================
def buscar_contato_por_documento(cpf_cnpj: str):
    cpf_cnpj = so_numeros(cpf_cnpj)

    if cpf_cnpj in CACHE_CONTATOS:
        return {
            "ok": True,
            "contato": CACHE_CONTATOS[cpf_cnpj],
            "motivo": "cache",
        }

    for pagina in range(1, MAX_PAGINAS_CONTATOS + 1):
        params = {
            "pagina": pagina,
            "limite": 100,
        }

        resp = request_bling("GET", "/contatos", params=params)
        if not resp:
            return {"ok": False, "erro": "falha_conexao"}

        print(f"Bling contatos: {resp.status_code} pagina {pagina}")

        if resp.status_code != 200:
            print("Erro contatos:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        data = resp.json().get("data", [])
        if not data:
            break

        for item in data:
            doc = so_numeros(item.get("numeroDocumento"))
            if doc == cpf_cnpj:
                CACHE_CONTATOS[cpf_cnpj] = item
                return {
                    "ok": True,
                    "contato": item,
                    "motivo": "encontrado",
                }

    return {"ok": False, "erro": "contato_nao_encontrado"}


# =========================
# CONTAS / BOLETOS
# =========================
def buscar_conta_receber_detalhe(id_conta: int):
    resp = request_bling("GET", f"/contas/receber/{id_conta}")
    if not resp:
        return None

    print(f"DETALHE_CONTA_STATUS: {id_conta} {resp.status_code}")

    if resp.status_code != 200:
        return None

    return (resp.json() or {}).get("data")


def conta_pertence_ao_pedido(conta: dict, pedido: str) -> bool:
    pedido = str(pedido).strip()

    origem = conta.get("origem") or {}
    detalhe = conta.get("_detalhe") or {}

    origem_numero = str(origem.get("numero") or "").strip()
    detalhe_numero_documento = str(detalhe.get("numeroDocumento") or "").strip()
    detalhe_historico = str(detalhe.get("historico") or "")

    if origem_numero == pedido:
        return True

    # fallback
    if pedido in detalhe_numero_documento:
        return True

    if pedido in detalhe_historico:
        return True

    return False


def boleto_valido(conta: dict) -> bool:
    detalhe = conta.get("_detalhe") or {}
    situacao = conta.get("situacao")
    link_boleto = conta.get("linkBoleto") or detalhe.get("linkBoleto") or ""
    saldo = detalhe.get("saldo", conta.get("valor", 0)) or 0

    print(
        "FILTRO_CONTA:",
        conta.get("id"),
        "pedido=", (conta.get("origem") or {}).get("numero", "Sem pedido"),
        "situacao=", situacao,
        "linkBoleto=", link_boleto,
        "saldo=", saldo,
        "historico=", detalhe.get("historico", ""),
    )

    if situacao not in SITUACOES_EM_ABERTO:
        return False

    if not link_boleto:
        return False

    if float(saldo) <= 0:
        return False

    return True


def ordenar_boletos(boletos: list):
    def chave(b):
        return (
            b.get("vencimento") or "9999-12-31",
            b.get("id") or 0,
        )

    return sorted(boletos, key=chave)


def buscar_boletos_por_contato(contato_id: int, pedido: Optional[str] = None):
    contas_map = {}

    # 1) Contas por contato
    for pagina in range(1, 3):
        params = {"pagina": pagina, "limite": 100, "idContato": contato_id}
        resp = request_bling("GET", "/contas/receber", params=params)
        if not resp:
            return {"ok": False, "erro": "falha_consulta"}

        print(f"Bling contas/receber por contato: {resp.status_code} pagina {pagina}")

        if resp.status_code != 200:
            print("Erro contas por contato:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        data = (resp.json() or {}).get("data", [])
        if not data:
            break

        for item in data:
            contas_map[item["id"]] = item

    # 2) Contas gerais
    for pagina in range(1, MAX_PAGINAS_CONTAS_GERAL + 1):
        params = {"pagina": pagina, "limite": 100}
        resp = request_bling("GET", "/contas/receber", params=params)
        if not resp:
            return {"ok": False, "erro": "falha_consulta"}

        print(f"Bling contas/receber geral: {resp.status_code} pagina {pagina}")

        if resp.status_code != 200:
            print("Erro contas geral:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        data = (resp.json() or {}).get("data", [])
        if not data:
            break

        for item in data:
            contato = item.get("contato") or {}
            if int(contato.get("id") or 0) == int(contato_id):
                contas_map[item["id"]] = item

    # 3) Varredura por pedido
    if pedido:
        for pagina in range(1, MAX_PAGINAS_CONTAS_POR_PEDIDO + 1):
            params = {"pagina": pagina, "limite": 100}
            resp = request_bling("GET", "/contas/receber", params=params)
            if not resp:
                return {"ok": False, "erro": "falha_consulta"}

            print(f"Bling contas/receber por pedido: {resp.status_code} pagina {pagina}")

            if resp.status_code != 200:
                print("Erro contas por pedido:", resp.text)
                return {"ok": False, "erro": "falha_consulta"}

            data = (resp.json() or {}).get("data", [])
            if not data:
                break

            achou_na_pagina = False

            for item in data:
                origem = item.get("origem") or {}
                origem_numero = str(origem.get("numero") or "").strip()
                contato = item.get("contato") or {}
                doc = so_numeros(contato.get("numeroDocumento"))

                if origem_numero == str(pedido).strip():
                    print(
                        "PEDIDO_ENCONTRADO_NA_VARREDURA:",
                        "pagina=", pagina,
                        "conta_id=", item.get("id"),
                        "pedido=", origem_numero,
                        "contato_id=", contato.get("id"),
                        "documento=", doc,
                        "situacao=", item.get("situacao"),
                    )
                    contas_map[item["id"]] = item
                    achou_na_pagina = True

            if achou_na_pagina and pagina > 1:
                pass

    contas = list(contas_map.values())
    print("TOTAL_CONTAS_COMBINADAS:", len(contas))

    boletos_validos = []
    for conta in contas:
        detalhe = buscar_conta_receber_detalhe(conta["id"])
        if not detalhe:
            continue

        conta["_detalhe"] = detalhe

        if pedido and not conta_pertence_ao_pedido(conta, pedido):
            continue

        if boleto_valido(conta):
            conta["_pedido_numero"] = str(((conta.get("origem") or {}).get("numero") or "")).strip()
            boletos_validos.append(conta)

    boletos_validos = ordenar_boletos(boletos_validos)
    return {"ok": True, "boletos": boletos_validos}


# =========================
# FORMATADORES
# =========================
def formatar_valor(valor):
    try:
        return f"R$ {float(valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return f"R$ {valor}"


def montar_lista_boletos(boletos: list) -> str:
    linhas = ["Encontrei os seguintes boletos:\n"]
    for i, b in enumerate(boletos, start=1):
        linhas.append(
            f"{i}️⃣ {formatar_valor(b.get('valor'))} - vence {b.get('vencimento')} - situação {b.get('situacao')}"
        )
    linhas.append("\nDigite o número ou TODOS.")
    return "\n".join(linhas)


# =========================
# WEBHOOK
# =========================
@app.get("/")
def root():
    return {"ok": True, "app": "boleto-webhook"}


@app.post("/webhook/digisac")
async def webhook(request: Request):
    payload = await request.json()
    print("Webhook recebido:", payload)

    if not eh_mensagem_cliente(payload):
        return {"ok": True, "ignorado": True}

    ticket_id = extrair_ticket_id(payload)
    contact_id = extrair_contact_id(payload)
    texto = extrair_texto(payload).strip()

    if not ticket_id or not contact_id:
        return {"ok": False, "erro": "ticket_ou_contact_ausente"}

    estado = obter_estado(ticket_id)
    texto_limpo = texto_normalizado(texto)
    numeros = so_numeros(texto)

    # =====================
    # INÍCIO DO FLUXO
    # =====================
    if texto_limpo == "teste boleto":
        limpar_estado(ticket_id)
        estado = obter_estado(ticket_id)
        estado["etapa"] = "aguardando_documento"
        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos.")
        return {"ok": True}

    # =====================
    # CPF / CNPJ
    # =====================
    if estado["etapa"] in ("aguardando_documento", "idle"):
        if len(numeros) not in (11, 14):
            enviar_mensagem(contact_id, "CPF ou CNPJ inválido. Digite apenas números.")
            return {"ok": True}

        resp_contato = buscar_contato_por_documento(numeros)
        print("RESPOSTA_CONTATO_DEBUG:", resp_contato)

        if not resp_contato["ok"]:
            enviar_mensagem(contact_id, "Não localizei cadastro para esse CPF/CNPJ.")
            return {"ok": True}

        estado["cpf_cnpj"] = numeros
        estado["contato_bling"] = resp_contato["contato"]
        estado["etapa"] = "aguardando_modo_consulta"

        enviar_mensagem(
            contact_id,
            "Como deseja consultar?\n\n1️⃣ Pedido específico\n2️⃣ Todos os boletos"
        )
        return {"ok": True}

    # =====================
    # ESCOLHA DO MODO
    # =====================
    if estado["etapa"] == "aguardando_modo_consulta":
        if texto_limpo == "1":
            estado["modo_consulta"] = "pedido"
            estado["etapa"] = "aguardando_numero_pedido"
            enviar_mensagem(contact_id, "Digite o número do pedido.")
            return {"ok": True}

        if texto_limpo == "2":
            estado["modo_consulta"] = "todos"
            estado["etapa"] = "consultando_todos"

            enviar_mensagem(contact_id, "Buscando seus boletos... 🔍")

            contato = estado["contato_bling"]
            resp_boletos = buscar_boletos_por_contato(contato["id"])
            print("RESPOSTA_BOLETOS_DEBUG:", resp_boletos)

            if not resp_boletos["ok"]:
                enviar_mensagem(contact_id, "Erro ao consultar boletos.")
                return {"ok": True}

            boletos = resp_boletos["boletos"]
            if not boletos:
                enviar_mensagem(contact_id, "Não encontrei boletos em aberto ou em atraso para esse cadastro.")
                limpar_estado(ticket_id)
                return {"ok": True}

            estado["boletos"] = boletos
            estado["etapa"] = "aguardando_escolha_boleto"
            enviar_mensagem(contact_id, montar_lista_boletos(boletos))
            return {"ok": True}

        enviar_mensagem(contact_id, "Digite 1 para pedido específico ou 2 para todos os boletos.")
        return {"ok": True}

    # =====================
    # NÚMERO DO PEDIDO
    # =====================
    if estado["etapa"] == "aguardando_numero_pedido":
        if not numeros:
            enviar_mensagem(contact_id, "Digite apenas o número do pedido.")
            return {"ok": True}

        estado["pedido"] = numeros
        estado["etapa"] = "consultando_pedido"

        enviar_mensagem(contact_id, "Buscando seu pedido... 🔍")

        contato = estado["contato_bling"]
        resp_boletos = buscar_boletos_por_contato(contato["id"], pedido=numeros)
        print("RESPOSTA_BOLETOS_DEBUG:", resp_boletos)

        if not resp_boletos["ok"]:
            enviar_mensagem(contact_id, "Erro ao consultar boletos.")
            return {"ok": True}

        boletos = resp_boletos["boletos"]
        if not boletos:
            enviar_mensagem(contact_id, "Não encontrei boleto para esse pedido.")
            limpar_estado(ticket_id)
            return {"ok": True}

        estado["boletos"] = boletos
        estado["etapa"] = "aguardando_escolha_boleto"
        enviar_mensagem(contact_id, montar_lista_boletos(boletos))
        return {"ok": True}

    # =====================
    # ESCOLHA DE BOLETO
    # =====================
    if estado["etapa"] == "aguardando_escolha_boleto":
        boletos = estado.get("boletos", [])

        if texto_limpo == "todos":
            enviados = 0
            for boleto in boletos:
                link = boleto.get("linkBoleto") or (boleto.get("_detalhe") or {}).get("linkBoleto")
                if link:
                    enviar_documento(contact_id, link)
                    enviados += 1

            if enviados == 0:
                enviar_mensagem(contact_id, "Não consegui obter os boletos.")
                limpar_estado(ticket_id)
                return {"ok": True}

            estado["etapa"] = "aguardando_encerramento"
            enviar_mensagem(contact_id, "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não")
            return {"ok": True}

        if not numeros:
            enviar_mensagem(contact_id, "Digite o número do boleto desejado ou TODOS.")
            return {"ok": True}

        indice = int(numeros) - 1
        if indice < 0 or indice >= len(boletos):
            enviar_mensagem(contact_id, "Número inválido. Escolha uma opção da lista.")
            return {"ok": True}

        boleto = boletos[indice]
        link = boleto.get("linkBoleto") or (boleto.get("_detalhe") or {}).get("linkBoleto")

        if not link:
            enviar_mensagem(contact_id, "Não consegui obter esse boleto.")
            limpar_estado(ticket_id)
            return {"ok": True}

        enviar_documento(contact_id, link)
        estado["etapa"] = "aguardando_encerramento"
        enviar_mensagem(contact_id, "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não")
        return {"ok": True}

    # =====================
    # ENCERRAMENTO
    # =====================
    if estado["etapa"] == "aguardando_encerramento":
        if texto_limpo == "1":
            enviar_mensagem(contact_id, "Atendimento encerrado. Obrigado!")
            limpar_estado(ticket_id)
            return {"ok": True}

        if texto_limpo == "2":
            enviar_mensagem(contact_id, "Certo. Se precisar de mais alguma coisa, é só me chamar.")
            limpar_estado(ticket_id)
            return {"ok": True}

        enviar_mensagem(contact_id, "Digite 1 para Sim ou 2 para Não.")
        return {"ok": True}

    return {"ok": True}