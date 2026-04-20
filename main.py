from fastapi import FastAPI, Request
import requests
import time

from auth_bling import obter_access_token, forcar_refresh
from config import DIGISAC_TOKEN, DIGISAC_BASE_URL, BLING_BASE_URL

app = FastAPI()

usuarios = {}
mensagens_processadas = set()

cache_contatos_por_doc = {}
cache_boletos_por_contato = {}

ULTIMA_CHAMADA_BLING = 0.0
INTERVALO_MINIMO_BLING = 0.4


# =====================================================
# UTIL
# =====================================================

def limpar_documento(doc):
    return "".join(ch for ch in str(doc or "") if ch.isdigit())


def formatar_valor(valor):
    try:
        return f"{float(valor):.2f}".replace(".", ",")
    except Exception:
        return str(valor)


def aguardar_limite_bling():
    global ULTIMA_CHAMADA_BLING

    agora = time.time()
    delta = agora - ULTIMA_CHAMADA_BLING

    if delta < INTERVALO_MINIMO_BLING:
        time.sleep(INTERVALO_MINIMO_BLING - delta)

    ULTIMA_CHAMADA_BLING = time.time()


# =====================================================
# DIGISAC
# =====================================================

def enviar_mensagem(contact_id, texto):
    url = f"{DIGISAC_BASE_URL}/messages"

    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "contactId": contact_id,
        "type": "chat",
        "text": texto
    }

    resp = requests.post(url, json=body, headers=headers, timeout=30)
    print("Digisac mensagem:", resp.status_code, resp.text)
    return resp


def enviar_documento(contact_id, url_pdf):
    url = f"{DIGISAC_BASE_URL}/messages"

    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "contactId": contact_id,
        "type": "document",
        "document": {
            "url": url_pdf
        }
    }

    resp = requests.post(url, json=body, headers=headers, timeout=30)
    print("Digisac documento:", resp.status_code, resp.text)
    return resp


# =====================================================
# BLING
# =====================================================

def bling_get(endpoint, params=None, retry_on_401=True, retry_on_429=2):
    aguardar_limite_bling()

    token = obter_access_token()

    headers = {
        "Authorization": f"Bearer {token}"
    }

    url = f"{BLING_BASE_URL}{endpoint}"

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 401 and retry_on_401:
        print("Token expirado. Renovando...")
        forcar_refresh()
        return bling_get(
            endpoint,
            params=params,
            retry_on_401=False,
            retry_on_429=retry_on_429
        )

    if resp.status_code == 429 and retry_on_429 > 0:
        print("Rate limit Bling. Aguardando 1s...")
        time.sleep(1)
        return bling_get(
            endpoint,
            params=params,
            retry_on_401=retry_on_401,
            retry_on_429=retry_on_429 - 1
        )

    return resp


# =====================================================
# BUSCA CONTATO
# =====================================================

def buscar_contato_por_documento(cpf_cnpj):
    cpf_cnpj = limpar_documento(cpf_cnpj)

    if cpf_cnpj in cache_contatos_por_doc:
        return {"ok": True, "contato": cache_contatos_por_doc[cpf_cnpj]}

    pagina = 1
    max_paginas = 20

    while pagina <= max_paginas:
        params = {
            "pagina": pagina,
            "limite": 100
        }

        resp = bling_get("/contatos", params=params)
        print("Bling contatos:", resp.status_code)

        if resp.status_code != 200:
            print("Erro contatos:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        data = resp.json().get("data", [])

        if pagina == 1 and data:
            print("AMOSTRA_CONTATO_1:", data[0])

        if not data:
            return {"ok": True, "contato": None}

        for contato in data:
            doc = limpar_documento(contato.get("numeroDocumento", ""))

            print(
                "CONTATO_DOC_DEBUG:",
                contato.get("id"),
                contato.get("nome"),
                doc
            )

            if doc == cpf_cnpj:
                cache_contatos_por_doc[cpf_cnpj] = contato
                return {"ok": True, "contato": contato}

        if len(data) < 100:
            return {"ok": True, "contato": None}

        pagina += 1

    return {"ok": False, "erro": "muitas_paginas"}


# =====================================================
# BUSCA BOLETOS
# =====================================================

def buscar_boletos_por_contato(contato_id):
    if contato_id in cache_boletos_por_contato:
        return {
            "ok": True,
            "boletos": cache_boletos_por_contato[contato_id]
        }

    pagina = 1
    encontrados = []

    while pagina <= 10:
        params = {
            "pagina": pagina,
            "limite": 100,
            "situacoes[]": [1, 3],
            "idContato": contato_id
        }

        resp = bling_get("/contas/receber", params=params)
        print("Bling contas/receber:", resp.status_code)

        if resp.status_code != 200:
            print("Erro contas:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        data = resp.json().get("data", [])

        print("CONTAS_RECEBER_RAW:", data[:5])

        if not data:
            break

        encontrados.extend(data)

        if len(data) < 100:
            break

        pagina += 1

    cache_boletos_por_contato[contato_id] = encontrados

    return {"ok": True, "boletos": encontrados}


def agrupar_boletos_por_pedido(lista):
    pedidos = {}

    for b in lista:
        numero = str(b.get("numeroDocumento", "Sem pedido")).strip()

        if "/" in numero:
            pedido = numero.split("/")[0].strip()
        else:
            pedido = numero or "Sem pedido"

        pedidos.setdefault(pedido, []).append(b)

    return pedidos


def buscar_link_boleto_por_conta(id_conta):
    resp = bling_get(f"/contas/receber/{id_conta}/boleto")
    print("Bling boleto:", resp.status_code, id_conta, resp.text)

    if resp.status_code != 200:
        return None

    data = resp.json().get("data", {})

    if not isinstance(data, dict):
        return None

    return (
        data.get("link")
        or data.get("url")
        or data.get("linkBoleto")
        or data.get("boleto")
    )


# =====================================================
# API
# =====================================================

@app.get("/")
def home():
    return {"status": "online"}


@app.post("/webhook/digisac")
async def webhook(request: Request):
    body = await request.json()
    print("Webhook recebido:", body)

    data = body.get("data", {})

    message_id = data.get("id")
    contact_id = data.get("contactId")
    is_from_me = data.get("isFromMe", True)
    message_type = data.get("type")

    mensagem_original = str(data.get("text", "")).strip()
    mensagem = mensagem_original.lower()
    comando = data.get("command") or mensagem_original

    if message_id in mensagens_processadas:
        return {"status": "ok"}

    if message_id:
        mensagens_processadas.add(message_id)

    if len(mensagens_processadas) > 5000:
        mensagens_processadas.clear()

    if not contact_id or is_from_me:
        return {"status": "ok"}

    if message_type != "chat":
        return {"status": "ok"}

    if not mensagem_original:
        return {"status": "ok"}

    estado = usuarios.get(contact_id, {}).get("estado")

    # =====================================================
    # GATILHO DE TESTE
    # =====================================================
    if mensagem == "teste boleto":
        usuarios[contact_id] = {"estado": "AGUARDANDO_CPF"}

        enviar_mensagem(
            contact_id,
            "Digite seu CPF ou CNPJ para localizar seus boletos."
        )
        return {"status": "ok"}

    # =====================================================
    # FLUXO OFICIAL (BOTÃO)
    # =====================================================
    if comando == "SEGUNDA_VIA":
        usuarios[contact_id] = {"estado": "AGUARDANDO_CPF"}

        enviar_mensagem(
            contact_id,
            "Digite seu CPF ou CNPJ para localizar seus boletos."
        )
        return {"status": "ok"}

    # =====================================================
    # CPF / CNPJ
    # =====================================================
    if estado == "AGUARDANDO_CPF":
        cpf = limpar_documento(mensagem)

        if len(cpf) not in (11, 14):
            enviar_mensagem(contact_id, "CPF ou CNPJ inválido.")
            return {"status": "ok"}

        enviar_mensagem(contact_id, "Buscando seus boletos... 🔍")

        resp_contato = buscar_contato_por_documento(cpf)

        if not resp_contato["ok"]:
            enviar_mensagem(
                contact_id,
                "Estou com instabilidade na consulta agora. Tente novamente em instantes."
            )
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        contato = resp_contato["contato"]

        if not contato:
            enviar_mensagem(contact_id, "Cadastro não localizado.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        print(
            "CONTATO_ENCONTRADO_ID:",
            contato.get("id"),
            contato.get("nome"),
            contato.get("numeroDocumento")
        )

        resp_boletos = buscar_boletos_por_contato(contato["id"])
        print("RESPOSTA_BOLETOS_DEBUG:", resp_boletos)

        if not resp_boletos["ok"]:
            enviar_mensagem(contact_id, "Erro ao consultar boletos.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        boletos = resp_boletos["boletos"]

        if not boletos:
            enviar_mensagem(
                contact_id,
                "Não encontrei boletos em aberto ou em atraso."
            )
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        pedidos = agrupar_boletos_por_pedido(boletos)

        texto = "Encontrei os seguintes pedidos:\n\n"
        mapa = {}

        for i, (pedido, lista) in enumerate(pedidos.items(), start=1):
            texto += f"{i}️⃣ Pedido {pedido} ({len(lista)} parcelas)\n"
            mapa[str(i)] = pedido

        texto += "\nDigite o número desejado."

        usuarios[contact_id] = {
            "estado": "AGUARDANDO_PEDIDO",
            "pedidos": pedidos,
            "mapa_pedidos": mapa
        }

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    # =====================================================
    # ESCOLHA DO PEDIDO
    # =====================================================
    if estado == "AGUARDANDO_PEDIDO":
        mapa = usuarios[contact_id]["mapa_pedidos"]
        pedido = mapa.get(mensagem)

        if not pedido:
            enviar_mensagem(contact_id, "Opção inválida.")
            return {"status": "ok"}

        boletos = usuarios[contact_id]["pedidos"][pedido]

        texto = "Boletos disponíveis:\n\n"
        mapa_boletos = {}

        for i, b in enumerate(boletos, start=1):
            valor = formatar_valor(b.get("valor", 0))
            venc = b.get("vencimento") or b.get("dataVencimento") or "-"

            texto += f"{i}️⃣ R$ {valor} - vence {venc}\n"
            mapa_boletos[str(i)] = b

        texto += "\nDigite o número ou TODOS."

        usuarios[contact_id]["estado"] = "AGUARDANDO_BOLETO"
        usuarios[contact_id]["mapa_boletos"] = mapa_boletos

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    # =====================================================
    # ESCOLHA DO BOLETO
    # =====================================================
    if estado == "AGUARDANDO_BOLETO":
        mapa = usuarios[contact_id]["mapa_boletos"]

        if mensagem == "todos":
            enviados = 0

            for boleto in mapa.values():
                id_conta = boleto.get("id")
                if not id_conta:
                    continue

                link = buscar_link_boleto_por_conta(id_conta)

                if link:
                    enviar_documento(contact_id, link)
                    enviados += 1

            if enviados == 0:
                enviar_mensagem(contact_id, "Não consegui obter os boletos.")
                return {"status": "ok"}

        elif mensagem in mapa:
            boleto = mapa[mensagem]
            id_conta = boleto.get("id")

            if not id_conta:
                enviar_mensagem(contact_id, "Não encontrei o identificador do boleto.")
                return {"status": "ok"}

            link = buscar_link_boleto_por_conta(id_conta)

            if not link:
                enviar_mensagem(contact_id, "Não consegui obter esse boleto.")
                return {"status": "ok"}

            enviar_documento(contact_id, link)

        else:
            enviar_mensagem(contact_id, "Opção inválida.")
            return {"status": "ok"}

        usuarios[contact_id]["estado"] = "FINALIZANDO"

        enviar_mensagem(
            contact_id,
            "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não"
        )
        return {"status": "ok"}

    # =====================================================
    # FINALIZAÇÃO
    # =====================================================
    if estado == "FINALIZANDO":
        if mensagem == "1":
            enviar_mensagem(contact_id, "Atendimento encerrado ✅")
            usuarios.pop(contact_id, None)

        elif mensagem == "2":
            enviar_mensagem(contact_id, "Vou transferir para o financeiro 👨‍💼")
            usuarios.pop(contact_id, None)

        else:
            enviar_mensagem(contact_id, "Digite 1 ou 2.")

        return {"status": "ok"}

    return {"status": "ok"}