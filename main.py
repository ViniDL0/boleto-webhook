from fastapi import FastAPI, Request
import requests
import time
import threading
import json
from collections import deque
from pathlib import Path
from auth_bling import obter_access_token, forcar_refresh
from config import DIGISAC_TOKEN, DIGISAC_BASE_URL, BLING_BASE_URL, DIGISAC_DEPARTMENT_ID_FINANCEIRO, DIGISAC_USER_ID_FINANCEIRO

app = FastAPI()

usuarios = {}
mensagens_processadas = set()

cache_contatos_por_doc = {}
cache_detalhe_conta = {}

BLING_MAX_REQ_POR_SEGUNDO = 3
BLING_JANELA_SEGUNDOS = 1.0
BLING_MAX_REQ_DIA = 110000
BLING_MAX_ERROS_10S = 250
BLING_JANELA_ERROS_SEGUNDOS = 10.0
BLING_COOLDOWN_ERROS_SEGUNDOS = 30
BLING_CONTADOR_ARQUIVO = Path("bling_rate_limit.json")

_bling_lock = threading.Lock()
_bling_requisicoes_1s = deque()
_bling_erros_10s = deque()


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


def _hoje_str():
    return time.strftime("%Y-%m-%d")


def _ler_contador_bling():
    if not BLING_CONTADOR_ARQUIVO.exists():
        return {"data": _hoje_str(), "total": 0}

    try:
        dados = json.loads(BLING_CONTADOR_ARQUIVO.read_text(encoding="utf-8"))
    except Exception:
        return {"data": _hoje_str(), "total": 0}

    if dados.get("data") != _hoje_str():
        return {"data": _hoje_str(), "total": 0}

    return {"data": dados.get("data", _hoje_str()), "total": int(dados.get("total", 0) or 0)}


def _salvar_contador_bling(dados):
    try:
        BLING_CONTADOR_ARQUIVO.write_text(json.dumps(dados), encoding="utf-8")
    except Exception as e:
        print("ERRO_SALVAR_CONTADOR_BLING:", e)


def aguardar_limite_bling():
    while True:
        with _bling_lock:
            agora = time.time()

            while _bling_requisicoes_1s and agora - _bling_requisicoes_1s[0] >= BLING_JANELA_SEGUNDOS:
                _bling_requisicoes_1s.popleft()

            dados_dia = _ler_contador_bling()
            if dados_dia["total"] >= BLING_MAX_REQ_DIA:
                raise RuntimeError("Limite diário preventivo do Bling atingido. Interrompendo para evitar bloqueio.")

            if len(_bling_requisicoes_1s) < BLING_MAX_REQ_POR_SEGUNDO:
                _bling_requisicoes_1s.append(agora)
                dados_dia["total"] += 1
                _salvar_contador_bling(dados_dia)
                return

            esperar = BLING_JANELA_SEGUNDOS - (agora - _bling_requisicoes_1s[0]) + 0.05

        time.sleep(max(esperar, 0.05))


def registrar_erro_bling(status_code):
    if status_code < 400:
        return

    with _bling_lock:
        agora = time.time()
        _bling_erros_10s.append(agora)

        while _bling_erros_10s and agora - _bling_erros_10s[0] >= BLING_JANELA_ERROS_SEGUNDOS:
            _bling_erros_10s.popleft()

        qtd_erros = len(_bling_erros_10s)

    if qtd_erros >= BLING_MAX_ERROS_10S:
        print(f"MUITOS_ERROS_BLING: {qtd_erros} erros em 10s. Pausando {BLING_COOLDOWN_ERROS_SEGUNDOS}s para evitar bloqueio de IP.")
        time.sleep(BLING_COOLDOWN_ERROS_SEGUNDOS)


def extrair_numero_contato_webhook(data):
    candidatos = [
        data.get("number"),
        data.get("fromNumber"),
        data.get("remoteJid"),
        data.get("phone"),
        data.get("mobile"),
        data.get("identifier"),
        data.get("from"),
        data.get("fromId"),
        data.get("contactId"),
    ]

    for c in candidatos:
        if c is None:
            continue
        valor = str(c).strip()
        if valor:
            return valor

    return ""


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

def fechar_chamado(contact_id):
    url = f"{DIGISAC_BASE_URL}/contacts/{contact_id}/ticket/close"

    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json"
    }

    resp = requests.post(url, headers=headers, timeout=30)

    print("Digisac fechar chamado:", resp.status_code, resp.text)
    return resp


def transferir_chamado(contact_id, department_id, user_id="", comments="Transferido pelo bot"):
    url = f"{DIGISAC_BASE_URL}/contacts/{contact_id}/ticket/transfer"

    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "departmentId": department_id,
        "comments": comments
    }

    if user_id:
        body["userId"] = user_id

    resp = requests.post(url, json=body, headers=headers, timeout=30)

    print("DIGISAC_TRANSFERENCIA_PAYLOAD:", body)
    print("Digisac transferência:", resp.status_code, resp.text)

    return resp


def enviar_link_boleto(contact_id, link_boleto, boleto=None):
    valor = formatar_valor((boleto or {}).get("valor", 0)) if boleto else ""
    venc = (boleto or {}).get("vencimento") or (boleto or {}).get("dataVencimento") or ""

    texto = "Segue o link do boleto solicitado:\n"

    if valor or venc:
        detalhes = []
        if valor:
            detalhes.append(f"Valor: R$ {valor}")
        if venc:
            detalhes.append(f"Vencimento: {venc}")
        texto += " | ".join(detalhes) + "\n"

    texto += str(link_boleto).strip()

    return enviar_mensagem(contact_id, texto)

def perguntar_continuar_atendimento(contact_id):
    usuarios[contact_id]["estado"] = "AGUARDANDO_CONTINUAR"

    enviar_mensagem(
        contact_id,
        "Como deseja continuar?\n\n"
        "1️⃣ Solicitar 2ª via de outro boleto\n"
        "2️⃣ Falar com atendente\n"
        "3️⃣ Encerrar atendimento"
    )


# =====================================================
# BLING
# =====================================================

def bling_get(endpoint, params=None, retry_on_401=True, retry_on_429=5, retry_on_5xx=3):
    try:
        aguardar_limite_bling()
    except RuntimeError as e:
        print("BLOQUEIO_PREVENTIVO_BLING:", e)
        class RespFake:
            status_code = 429
            text = str(e)
            def json(self):
                return {"error": {"message": str(e)}}
        return RespFake()

    token = obter_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "enable-jwt": "1"
    }

    url = f"{BLING_BASE_URL}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    registrar_erro_bling(resp.status_code)

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
        retry_after = resp.headers.get("Retry-After")
        try:
            espera = float(retry_after) if retry_after else None
        except Exception:
            espera = None

        if espera is None:
            tentativa = 6 - retry_on_429
            espera = min(2 ** tentativa, 30)

        print(f"Rate limit Bling 429. Aguardando {espera}s antes de tentar novamente...")
        time.sleep(espera)
        return bling_get(
            endpoint,
            params=params,
            retry_on_401=retry_on_401,
            retry_on_429=retry_on_429 - 1,
            retry_on_5xx=retry_on_5xx
        )

    if resp.status_code in (502, 503, 504) and retry_on_5xx > 0:
        tentativa = 4 - retry_on_5xx
        espera = min(2 ** tentativa, 20)
        print(f"Erro {resp.status_code} no Bling. Aguardando {espera}s para tentar novamente...")
        time.sleep(espera)
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
    service_id = data.get("serviceId")
    numero_contato = extrair_numero_contato_webhook(data)

    is_from_me = data.get("isFromMe", True)
    message_type = data.get("type")

    mensagem_original = str(data.get("text", "")).strip()
    mensagem = mensagem_original.lower()
    comando = data.get("command") or mensagem_original

    comando_identificador = (
        data.get("data", {}).get("commandIdentifier")
        or data.get("commandIdentifier")
        or data.get("identifier")
        or comando
    )

    if (
        comando_identificador == "segunda_via_boleto"
        or mensagem in ["2ª via de boletos"]
    ):
        usuarios[contact_id] = {
            "estado": "AGUARDANDO_CPF",
            "service_id": service_id,
            "numero_contato": numero_contato
        }

        enviar_mensagem(
            contact_id,
            "Digite seu CPF ou CNPJ para localizar seus boletos."
        )

        return {"status": "ok"}

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
        usuarios[contact_id] = {
            "estado": "AGUARDANDO_CPF",
            "service_id": service_id,
            "numero_contato": numero_contato
        }
        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos.")
        return {"status": "ok"}

    if comando == "SEGUNDA_VIA":
        usuarios[contact_id] = {
            "estado": "AGUARDANDO_CPF",
            "service_id": service_id,
            "numero_contato": numero_contato
        }
        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos.")
        return {"status": "ok"}

    if estado == "AGUARDANDO_CPF":
        cpf = limpar_documento(mensagem)

        if len(cpf) not in (11, 14):
            enviar_mensagem(contact_id, "CPF ou CNPJ inválido. Digite novamente somente os números.")
            return {"status": "ok"}

        enviar_mensagem(contact_id, "Consultando cadastro... 🔍")

        resp_contato = buscar_contato_por_documento(cpf)
        print("RESPOSTA_CONTATO_DEBUG:", resp_contato)

        if not resp_contato["ok"]:
            enviar_mensagem(contact_id, "Estou com instabilidade na consulta agora.")
            perguntar_continuar_atendimento(contact_id)
            return {"status": "ok"}

        contato = resp_contato["contato"]

        if not contato:
            enviar_mensagem(contact_id, "Cadastro não localizado para esse CPF/CNPJ.")
            perguntar_continuar_atendimento(contact_id)
            return {"status": "ok"}

        usuarios[contact_id] = {
            "estado": "BUSCANDO_BOLETOS",
            "cpf": cpf,
            "contato_id": contato.get("id"),
            "service_id": usuarios.get(contact_id, {}).get("service_id") or service_id,
            "numero_contato": usuarios.get(contact_id, {}).get("numero_contato") or numero_contato
        }

        enviar_mensagem(contact_id, "Buscando seus boletos... 🔍")

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
            perguntar_continuar_atendimento(contact_id)
            return {"status": "ok"}

        boletos = resp_boletos["boletos"]

        if not boletos:
            enviar_mensagem(contact_id, "Não encontrei boletos em aberto ou em atraso para esse cadastro.")
            perguntar_continuar_atendimento(contact_id)
            return {"status": "ok"}

        texto = "Encontrei os seguintes boletos:\n\n"
        mapa_boletos = {}

        for i, b in enumerate(boletos, start=1):
            valor = formatar_valor(b.get("valor", 0))
            venc = b.get("vencimento") or b.get("dataVencimento") or "-"
            situacao = b.get("situacao", "-")
            pedido = b.get("_pedido_numero") or extrair_numero_pedido(b)

            if pedido and pedido != "Sem pedido":
                texto += f"{i}️⃣ Pedido {pedido} - R$ {valor} - vence {venc} - situação {situacao}\n"
            else:
                texto += f"{i}️⃣ R$ {valor} - vence {venc} - situação {situacao}\n"

            mapa_boletos[str(i)] = b

        texto += "\nDigite o número do boleto ou TODOS."

        usuarios[contact_id] = {
            "estado": "AGUARDANDO_BOLETO",
            "mapa_boletos": mapa_boletos,
            "service_id": usuarios.get(contact_id, {}).get("service_id") or service_id,
            "numero_contato": usuarios.get(contact_id, {}).get("numero_contato") or numero_contato
        }

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    if estado == "AGUARDANDO_BOLETO":
        mapa = usuarios[contact_id]["mapa_boletos"]
        if mensagem == "todos":
            enviados = 0

            for idx, boleto in mapa.items():
                link = buscar_link_boleto_do_item(boleto)

                if not link:
                    print(f"BOLETO_SEM_LINK: opcao={idx} id={boleto.get('id')}")
                    continue

                resp_link = enviar_link_boleto(contact_id, link, boleto=boleto)

                if resp_link and resp_link.status_code in (200, 201):
                    enviados += 1
                else:
                    if resp_link is None:
                        print("ERRO_ENVIO_LINK: resposta None")
                    else:
                        print("ERRO_ENVIO_LINK:", resp_link.status_code, resp_link.text)

            if enviados == 0:
                enviar_mensagem(contact_id, "Não consegui enviar os boletos.")
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

            resp_link = enviar_link_boleto(contact_id, link, boleto=boleto)

            if not resp_link or resp_link.status_code not in (200, 201):
                if resp_link is None:
                    print("ERRO_ENVIO_LINK: resposta None")
                else:
                    print("ERRO_ENVIO_LINK:", resp_link.status_code, resp_link.text)
                enviar_mensagem(contact_id, "Não consegui enviar o link desse boleto.")
                return {"status": "ok"}

            usuarios[contact_id]["estado"] = "AGUARDANDO_ENCERRAR"
            enviar_mensagem(
                contact_id,
                "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não"
            )
            return {"status": "ok"}

        else:
            enviar_mensagem(contact_id, "Opção inválida. Digite o número do boleto ou TODOS.")
            return {"status": "ok"}

    if estado == "AGUARDANDO_ENCERRAR":

        if mensagem == "1":
            enviar_mensagem(contact_id, "Atendimento encerrado ✅")
            fechar_chamado(contact_id)
            usuarios.pop(contact_id, None)

        elif mensagem == "2":
            enviar_mensagem(
                contact_id,
                "Como deseja continuar?\n\n1️⃣ Solicitar 2ª via de outro boleto\n2️⃣ Falar com atendente"
            )
            usuarios[contact_id]["estado"] = "AGUARDANDO_CONTINUAR"

        else:
            enviar_mensagem(contact_id, "Digite 1 ou 2.")

        return {"status": "ok"}


    if estado == "AGUARDANDO_CONTINUAR":

        if mensagem == "1":
            usuarios[contact_id] = {
                "estado": "AGUARDANDO_CPF",
                "service_id": service_id,
                "numero_contato": numero_contato
            }

            enviar_mensagem(
                contact_id,
                "Digite seu CPF ou CNPJ para localizar seus boletos."
            )

            return {"status": "ok"}

        elif mensagem == "2":
            enviar_mensagem(contact_id, "Vou transferir para o financeiro 👨‍💼")

            transferir_chamado(
                contact_id=contact_id,
                department_id=DIGISAC_DEPARTMENT_ID_FINANCEIRO,
                user_id=DIGISAC_USER_ID_FINANCEIRO,
                comments="Cliente solicitou atendimento humano após consulta de boleto."
            )

            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        elif mensagem == "3":
            enviar_mensagem(contact_id, "Atendimento encerrado ✅")
            fechar_chamado(contact_id)
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        else:
            enviar_mensagem(
                contact_id,
                "Digite 1, 2 ou 3.\n\n"
                "1️⃣ Solicitar 2ª via de outro boleto\n"
                "2️⃣ Falar com atendente\n"
                "3️⃣ Encerrar atendimento"
            )

            return {"status": "ok"}