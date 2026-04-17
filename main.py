from fastapi import FastAPI, Request
import requests

from auth_bling import obter_access_token, forcar_refresh
from config import DIGISAC_TOKEN, BLING_BASE_URL

app = FastAPI()

usuarios = {}


def enviar_mensagem(contact_id, texto):
    url = "https://api.digisac.co/v1/messages"
    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "contactId": contact_id,
        "type": "text",
        "text": texto,
    }

    resp = requests.post(url, json=body, headers=headers, timeout=30)
    print("Digisac mensagem:", resp.status_code, resp.text)


def enviar_documento(contact_id, url_pdf):
    url = "https://api.digisac.co/v1/messages"
    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "contactId": contact_id,
        "type": "document",
        "document": {"url": url_pdf},
    }

    resp = requests.post(url, json=body, headers=headers, timeout=30)
    print("Digisac documento:", resp.status_code, resp.text)


def bling_get(endpoint, params=None, retry_on_401=True):
    token = obter_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{BLING_BASE_URL}{endpoint}"

    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 401 and retry_on_401:
        print("Token Bling expirado, renovando...")
        forcar_refresh()
        return bling_get(endpoint, params=params, retry_on_401=False)

    return resp


def limpar_documento(doc: str) -> str:
    return "".join(ch for ch in str(doc or "") if ch.isdigit())


def buscar_boletos_por_cpf(cpf_cnpj):
    cpf_cnpj = limpar_documento(cpf_cnpj)
    pagina = 1
    encontrados = []

    while True:
        params = {
            "pagina": pagina,
            "limite": 100,
            "situacoes[]": [1, 3],  # aberto + atrasado
        }

        resp = bling_get("/contas/receber", params=params)
        print("Bling contas/receber:", resp.status_code)

        if resp.status_code != 200:
            print("Erro Bling contas/receber:", resp.text)
            return []

        data = resp.json().get("data", [])
        if not data:
            break

        for item in data:
            contato = item.get("contato", {}) or {}
            doc = limpar_documento(contato.get("numeroDocumento", ""))

            if doc == cpf_cnpj:
                encontrados.append(item)

        if len(data) < 100:
            break

        pagina += 1

    return encontrados


def agrupar_boletos_por_pedido(lista):
    pedidos = {}

    for b in lista:
        numero = str(b.get("numeroDocumento", "Sem pedido"))

        if "/" in numero:
            pedido = numero.split("/")[0].strip()
        else:
            pedido = numero.strip()

        pedidos.setdefault(pedido, []).append(b)

    return pedidos


def buscar_link_boleto_por_conta(id_conta):
    resp = bling_get(f"/contas/receber/{id_conta}/boleto")
    print(f"Bling boleto {id_conta}:", resp.status_code, resp.text)

    if resp.status_code != 200:
        return None

    payload = resp.json()
    data = payload.get("data", {}) if isinstance(payload, dict) else {}

    if isinstance(data, dict):
        return (
            data.get("link")
            or data.get("url")
            or data.get("linkBoleto")
            or data.get("boleto")
        )

    return None


@app.get("/")
def home():
    return {"status": "online"}


@app.post("/webhook/digisac")
async def webhook(request: Request):
    body = await request.json()
    print("Webhook recebido:", body)

    data = body.get("data", {})
    contact_id = data.get("contactId")
    mensagem = str(data.get("text", "")).strip().lower()
    is_from_me = data.get("isFromMe", True)
    comando = data.get("command") or str(data.get("text", "")).strip()

    if not contact_id or is_from_me:
        return {"status": "ok"}

    estado = usuarios.get(contact_id, {}).get("estado")

    if comando == "SEGUNDA_VIA":
        usuarios[contact_id] = {"estado": "AGUARDANDO_CPF"}
        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos.")
        return {"status": "ok"}

    if estado == "AGUARDANDO_CPF":
        cpf = limpar_documento(mensagem)

        if len(cpf) not in (11, 14):
            enviar_mensagem(contact_id, "CPF ou CNPJ inválido. Digite apenas os números.")
            return {"status": "ok"}

        enviar_mensagem(contact_id, "Buscando seus boletos... 🔍")
        boletos = buscar_boletos_por_cpf(cpf)

        if not boletos:
            enviar_mensagem(contact_id, "Não encontrei boletos em aberto ou em atraso para esse CPF/CNPJ.")
            usuarios.pop(contact_id, None)
            return {"status": "ok"}

        pedidos = agrupar_boletos_por_pedido(boletos)
        mapa_pedidos = {}

        texto = "Encontrei os seguintes pedidos:\n\n"
        for i, (pedido, lista) in enumerate(pedidos.items(), start=1):
            texto += f"{i}️⃣ Pedido {pedido} ({len(lista)} parcelas)\n"
            mapa_pedidos[str(i)] = pedido

        texto += "\nDigite o número do pedido desejado."

        usuarios[contact_id] = {
            "estado": "AGUARDANDO_PEDIDO",
            "pedidos": pedidos,
            "mapa_pedidos": mapa_pedidos,
        }

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    if estado == "AGUARDANDO_PEDIDO":
        mapa_pedidos = usuarios[contact_id].get("mapa_pedidos", {})
        pedido_escolhido = mapa_pedidos.get(mensagem)

        if not pedido_escolhido:
            enviar_mensagem(contact_id, "Opção inválida. Digite o número do pedido.")
            return {"status": "ok"}

        boletos = usuarios[contact_id]["pedidos"][pedido_escolhido]
        mapa_boletos = {}

        texto = "Boletos disponíveis:\n\n"
        for i, b in enumerate(boletos, start=1):
            valor = b.get("valor", 0)
            vencimento = b.get("vencimento") or b.get("dataVencimento") or "-"
            texto += f"{i}️⃣ R$ {valor} - vence {vencimento}\n"
            mapa_boletos[str(i)] = b

        texto += "\nDigite o número do boleto ou TODOS."

        usuarios[contact_id]["estado"] = "AGUARDANDO_BOLETO"
        usuarios[contact_id]["mapa_boletos"] = mapa_boletos

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    if estado == "AGUARDANDO_BOLETO":
        mapa_boletos = usuarios[contact_id].get("mapa_boletos", {})

        if mensagem == "todos":
            enviados = 0
            for boleto in mapa_boletos.values():
                id_conta = boleto.get("id")
                if not id_conta:
                    continue

                link = buscar_link_boleto_por_conta(id_conta)
                if link:
                    enviar_documento(contact_id, link)
                    enviados += 1

            if enviados == 0:
                enviar_mensagem(contact_id, "Não consegui obter os links dos boletos.")
                return {"status": "ok"}

        elif mensagem in mapa_boletos:
            boleto = mapa_boletos[mensagem]
            id_conta = boleto.get("id")

            if not id_conta:
                enviar_mensagem(contact_id, "Não encontrei o identificador do boleto.")
                return {"status": "ok"}

            link = buscar_link_boleto_por_conta(id_conta)
            if not link:
                enviar_mensagem(contact_id, "Não consegui obter o link desse boleto.")
                return {"status": "ok"}

            enviar_documento(contact_id, link)

        else:
            enviar_mensagem(contact_id, "Opção inválida. Digite o número do boleto ou TODOS.")
            return {"status": "ok"}

        usuarios[contact_id]["estado"] = "FINALIZANDO"
        enviar_mensagem(contact_id, "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não")
        return {"status": "ok"}

    if estado == "FINALIZANDO":
        if mensagem == "1":
            enviar_mensagem(contact_id, "Atendimento encerrado ✅")
            usuarios.pop(contact_id, None)
        elif mensagem == "2":
            enviar_mensagem(contact_id, "Certo. Vou direcionar seu atendimento para o financeiro.")
            usuarios.pop(contact_id, None)
        else:
            enviar_mensagem(contact_id, "Digite 1 para encerrar ou 2 para continuar.")

        return {"status": "ok"}

    return {"status": "ok"}