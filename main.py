from fastapi import FastAPI, Request
import requests
import time

from auth_bling import obter_access_token, forcar_refresh
from config import DIGISAC_TOKEN, DIGISAC_BASE_URL, BLING_BASE_URL

app = FastAPI()

usuarios = {}
mensagens_processadas = set()

cache_contatos_por_doc = {}
cache_detalhe_conta = {}

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


def enviar_documento(contact_id, url_pdf, filename="boleto.pdf"):
    url = f"{DIGISAC_BASE_URL}/messages"

    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "contactId": contact_id,
        "type": "file",
        "file": {
            "url": url_pdf,
            "name": filename
        }
    }

    resp = requests.post(url, json=body, headers=headers, timeout=30)
    print("Digisac documento:", resp.status_code, resp.text)
    return resp


# =====================================================
# BLING
# =====================================================

def bling_get(endpoint, params=None, retry_on_401=True, retry_on_429=2, retry_on_5xx=2):
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
            retry_on_429=retry_on_429,
            retry_on_5xx=retry_on_5xx
        )

    if resp.status_code == 429 and retry_on_429 > 0:
        print("Rate limit Bling. Aguardando 1s...")
        time.sleep(1)
        return bling_get(
            endpoint,
            params=params,
            retry_on_401=retry_on_401,
            retry_on_429=retry_on_429 - 1,
            retry_on_5xx=retry_on_5xx
        )

    if resp.status_code in (502, 503, 504) and retry_on_5xx > 0:
        print(f"Erro {resp.status_code} no Bling. Aguardando 2s para tentar novamente...")
        time.sleep(2)
        return bling_get(
            endpoint,
            params=params,
            retry_on_401=retry_on_401,
            retry_on_429=retry_on_429,
            retry_on_5xx=retry_on_5xx - 1
        )

    return resp


def buscar_detalhe_conta(id_conta):
    if id_conta in cache_detalhe_conta:
        return cache_detalhe_conta[id_conta]

    resp = bling_get(f"/contas/receber/{id_conta}")
    print("DETALHE_CONTA_STATUS:", id_conta, resp.status_code)

    if resp.status_code != 200:
        print("Erro detalhe conta:", resp.text)
        return None

    data = resp.json().get("data", {})
    cache_detalhe_conta[id_conta] = data
    return data


# =====================================================
# REGRAS DE NEGÓCIO
# =====================================================

def extrair_numero_pedido(conta):
    origem = conta.get("origem", {}) or {}
    numero_pedido = str(origem.get("numero", "")).strip()
    return numero_pedido or "Sem pedido"


def conta_em_aberto(conta):
    try:
        situacao = int(conta.get("situacao", 0) or 0)
    except Exception:
        situacao = 0

    return situacao in (1, 3)


def conta_e_boleto(detalhe):
    link_boleto = str((detalhe or {}).get("linkBoleto", "") or "").strip()
    return bool(link_boleto)


def deduplicar_contas(contas):
    unicas = {}
    for conta in contas:
        conta_id = conta.get("id")
        if conta_id:
            unicas[conta_id] = conta
    return list(unicas.values())


def buscar_link_boleto_do_item(boleto):
    detalhe = boleto.get("_detalhe", {}) or {}
    link = str(detalhe.get("linkBoleto", "") or "").strip()

    if link:
        return link

    link = str(boleto.get("linkBoleto", "") or "").strip()
    if link:
        return link

    return None


# =====================================================
# BUSCA CONTATO
# =====================================================

def buscar_contato_por_documento(cpf_cnpj):
    cpf_cnpj = limpar_documento(cpf_cnpj)

    if cpf_cnpj in cache_contatos_por_doc:
        return {
            "ok": True,
            "contato": cache_contatos_por_doc[cpf_cnpj],
            "motivo": "cache"
        }

    pagina = 1
    max_paginas = 220

    while pagina <= max_paginas:
        params = {
            "pagina": pagina,
            "limite": 100
        }

        resp = bling_get("/contatos", params=params)
        print("Bling contatos:", resp.status_code, "pagina", pagina)

        if resp.status_code != 200:
            print("Erro contatos:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        data = resp.json().get("data", [])

        if not data:
            return {"ok": True, "contato": None, "motivo": "nao_encontrado"}

        for contato in data:
            doc = limpar_documento(contato.get("numeroDocumento", ""))

            if doc == cpf_cnpj:
                cache_contatos_por_doc[cpf_cnpj] = contato
                return {"ok": True, "contato": contato, "motivo": "encontrado"}

        if len(data) < 100:
            return {"ok": True, "contato": None, "motivo": "nao_encontrado"}

        pagina += 1

    return {"ok": True, "contato": None, "motivo": "limite_paginas"}


# =====================================================
# BUSCA CONTAS
# =====================================================

def buscar_contas_por_contato_id(contato_id):
    pagina = 1
    contas = []

    while pagina <= 10:
        params = {
            "pagina": pagina,
            "limite": 100,
            "idContato": contato_id
        }

        resp = bling_get("/contas/receber", params=params)
        print("Bling contas/receber por contato:", resp.status_code, "pagina", pagina)

        if resp.status_code != 200:
            print("Erro contas por contato:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        dados = resp.json().get("data", [])

        if not dados:
            break

        contas.extend(dados)

        if len(dados) < 100:
            break

        pagina += 1

    return {"ok": True, "contas": contas}


def buscar_contas_por_documento(cpf_cnpj):
    cpf_cnpj = limpar_documento(cpf_cnpj)
    pagina = 1
    contas = []

    while pagina <= 20:
        params = {
            "pagina": pagina,
            "limite": 100
        }

        resp = bling_get("/contas/receber", params=params)
        print("Bling contas/receber geral:", resp.status_code, "pagina", pagina)

        if resp.status_code != 200:
            print("Erro contas geral:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        dados = resp.json().get("data", [])

        if not dados:
            break

        for conta in dados:
            contato = conta.get("contato", {}) or {}
            doc = limpar_documento(contato.get("numeroDocumento", ""))

            if doc == cpf_cnpj:
                contas.append(conta)

        if len(dados) < 100:
            break

        pagina += 1

    return {"ok": True, "contas": contas}


def buscar_contas_por_numero_pedido(numero_pedido):
    numero_pedido = str(numero_pedido).strip()
    pagina = 1
    contas = []

    while pagina <= 60:
        params = {
            "pagina": pagina,
            "limite": 100
        }

        resp = bling_get("/contas/receber", params=params)
        print("Bling contas/receber por pedido:", resp.status_code, "pagina", pagina)

        if resp.status_code != 200:
            print("Erro contas por pedido:", resp.text)
            return {"ok": False, "erro": "falha_consulta"}

        dados = resp.json().get("data", [])

        if not dados:
            break

        for conta in dados:
            origem = conta.get("origem", {}) or {}
            pedido = str(origem.get("numero", "")).strip()

            if pedido:
                print(
                    "PEDIDO_ENCONTRADO_NA_VARREDURA:",
                    "pagina=", pagina,
                    "conta_id=", conta.get("id"),
                    "pedido=", pedido,
                    "contato_id=", (conta.get("contato") or {}).get("id"),
                    "documento=", (conta.get("contato") or {}).get("numeroDocumento"),
                    "situacao=", conta.get("situacao")
                )

            if pedido == numero_pedido:
                print("MATCH_PEDIDO:", conta)
                contas.append(conta)

        if len(dados) < 100:
            break

        pagina += 1

    return {"ok": True, "contas": contas}


def filtrar_boletos(contas, numero_pedido=None):
    boletos = []
    numero_pedido = str(numero_pedido).strip() if numero_pedido else None

    for conta in contas:
        detalhe = buscar_detalhe_conta(conta.get("id"))
        pedido = extrair_numero_pedido(conta)

        print(
            "FILTRO_CONTA:",
            conta.get("id"),
            "pedido=", pedido,
            "situacao=", conta.get("situacao"),
            "linkBoleto=", (detalhe or {}).get("linkBoleto", ""),
            "saldo=", (detalhe or {}).get("saldo", ""),
            "historico=", (detalhe or {}).get("historico", "")
        )

        if numero_pedido and pedido != numero_pedido:
            continue

        if not detalhe:
            continue

        if not conta_em_aberto(conta):
            continue

        if not conta_e_boleto(detalhe):
            continue

        conta["_detalhe"] = detalhe
        conta["_pedido_numero"] = pedido
        boletos.append(conta)

    return boletos


def buscar_boletos_completo(contato_id, cpf_cnpj, numero_pedido=None):
    resp1 = buscar_contas_por_contato_id(contato_id)
    if not resp1["ok"]:
        return {"ok": False, "erro": "falha_consulta"}

    resp2 = buscar_contas_por_documento(cpf_cnpj)
    if not resp2["ok"]:
        return {"ok": False, "erro": "falha_consulta"}

    contas = resp1["contas"] + resp2["contas"]

    if numero_pedido:
        resp3 = buscar_contas_por_numero_pedido(numero_pedido)
        if not resp3["ok"]:
            return {"ok": False, "erro": "falha_consulta"}
        contas += resp3["contas"]

    contas = deduplicar_contas(contas)
    print("TOTAL_CONTAS_COMBINADAS:", len(contas))

    boletos = filtrar_boletos(contas, numero_pedido=numero_pedido)
    return {"ok": True, "boletos": boletos}


def agrupar_boletos_por_pedido(lista):
    pedidos = {}

    for b in lista:
        pedido = str(b.get("_pedido_numero") or "Sem pedido").strip()
        pedidos.setdefault(pedido, []).append(b)

    return pedidos


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

    if mensagem == "teste boleto":
        usuarios[contact_id] = {"estado": "AGUARDANDO_CPF"}
        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos.")
        return {"status": "ok"}

    if comando == "SEGUNDA_VIA":
        usuarios[contact_id] = {"estado": "AGUARDANDO_CPF"}
        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos.")
        return {"status": "ok"}

    if estado == "AGUARDANDO_CPF":
        cpf = limpar_documento(mensagem)

        if len(cpf) not in (11, 14):
            enviar_mensagem(contact_id, "CPF ou CNPJ inválido.")
            return {"status": "ok"}

        usuarios[contact_id] = {
            "estado": "AGUARDANDO_MODO_BUSCA",
            "cpf": cpf
        }

        enviar_mensagem(
            contact_id,
            "Como deseja consultar?\n\n"
            "1️⃣ Pedido específico\n"
            "2️⃣ Todos os boletos"
        )
        return {"status": "ok"}

    if estado == "AGUARDANDO_MODO_BUSCA":
        if mensagem == "1":
            usuarios[contact_id]["estado"] = "AGUARDANDO_NUMERO_PEDIDO"
            enviar_mensagem(contact_id, "Digite o número do pedido.")
            return {"status": "ok"}

        if mensagem == "2":
            cpf = usuarios[contact_id]["cpf"]

            enviar_mensagem(contact_id, "Buscando seus boletos... 🔍")

            resp_contato = buscar_contato_por_documento(cpf)
            print("RESPOSTA_CONTATO_DEBUG:", resp_contato)

            if not resp_contato["ok"]:
                enviar_mensagem(contact_id, "Estou com instabilidade na consulta agora. Tente novamente em instantes.")
                usuarios.pop(contact_id, None)
                return {"status": "ok"}

            contato = resp_contato["contato"]

            if not contato:
                enviar_mensagem(contact_id, "Cadastro não localizado para esse CPF/CNPJ.")
                usuarios.pop(contact_id, None)
                return {"status": "ok"}

            print(
                "CONTATO_ENCONTRADO_ID:",
                contato.get("id"),
                contato.get("nome"),
                contato.get("numeroDocumento")
            )

            resp_boletos = buscar_boletos_completo(contato["id"], cpf)
            print("RESPOSTA_BOLETOS_DEBUG:", resp_boletos)

            if not resp_boletos["ok"]:
                enviar_mensagem(contact_id, "Erro ao consultar boletos.")
                usuarios.pop(contact_id, None)
                return {"status": "ok"}

            boletos = resp_boletos["boletos"]

            if not boletos:
                enviar_mensagem(contact_id, "Não encontrei boletos em aberto ou em atraso para esse cadastro.")
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

        enviar_mensagem(contact_id, "Digite 1 ou 2.")
        return {"status": "ok"}

    if estado == "AGUARDANDO_NUMERO_PEDIDO":
        numero_pedido = mensagem.strip()
        cpf = usuarios[contact_id]["cpf"]

        enviar_mensagem(contact_id, "Buscando seu pedido... 🔍")

        resp_contato = buscar_contato_por_documento(cpf)
        print("RESPOSTA_CONTATO_DEBUG:", resp_contato)

        if not resp_contato["ok"]:
            enviar_mensagem(contact_id, "Erro ao consultar cadastro.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        contato = resp_contato["contato"]

        if not contato:
            enviar_mensagem(contact_id, "Cadastro não localizado para esse CPF/CNPJ.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        resp_boletos = buscar_boletos_completo(
            contato["id"],
            cpf,
            numero_pedido=numero_pedido
        )
        print("RESPOSTA_BOLETOS_DEBUG:", resp_boletos)

        if not resp_boletos["ok"]:
            enviar_mensagem(contact_id, "Erro ao consultar boletos.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        boletos = resp_boletos["boletos"]

        if not boletos:
            enviar_mensagem(contact_id, "Não encontrei boleto para esse pedido.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        texto = "Encontrei os seguintes boletos:\n\n"
        mapa_boletos = {}

        for i, b in enumerate(boletos, start=1):
            valor = formatar_valor(b.get("valor", 0))
            venc = b.get("vencimento") or b.get("dataVencimento") or "-"
            situacao = b.get("situacao", "-")
            texto += f"{i}️⃣ R$ {valor} - vence {venc} - situação {situacao}\n"
            mapa_boletos[str(i)] = b

        texto += "\nDigite o número ou TODOS."

        usuarios[contact_id] = {
            "estado": "AGUARDANDO_BOLETO",
            "mapa_boletos": mapa_boletos
        }

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

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
            situacao = b.get("situacao", "-")

            texto += f"{i}️⃣ R$ {valor} - vence {venc} - situação {situacao}\n"
            mapa_boletos[str(i)] = b

        texto += "\nDigite o número ou TODOS."

        usuarios[contact_id]["estado"] = "AGUARDANDO_BOLETO"
        usuarios[contact_id]["mapa_boletos"] = mapa_boletos

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    if estado == "AGUARDANDO_BOLETO":
        mapa = usuarios[contact_id]["mapa_boletos"]

        if mensagem == "todos":
            enviados = 0

            for boleto in mapa.values():
                link = buscar_link_boleto_do_item(boleto)

                if link:
                    enviar_documento(contact_id, link)
                    enviados += 1

            if enviados == 0:
                enviar_mensagem(contact_id, "Não consegui obter os boletos.")
                return {"status": "ok"}

            usuarios[contact_id]["estado"] = "AGUARDANDO_ENCERRAR"
            enviar_mensagem(
                contact_id,
                "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não"
            )
            return {"status": "ok"}

        elif mensagem in mapa:
            boleto = mapa[mensagem]
            link = buscar_link_boleto_do_item(boleto)

            if not link:
                enviar_mensagem(contact_id, "Não consegui obter esse boleto.")
                return {"status": "ok"}

            enviar_documento(contact_id, link)

            usuarios[contact_id]["estado"] = "AGUARDANDO_ENCERRAR"
            enviar_mensagem(
                contact_id,
                "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não"
            )
            return {"status": "ok"}

        else:
            enviar_mensagem(contact_id, "Opção inválida. Digite o número do boleto ou TODOS.")
            return {"status": "ok"}

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