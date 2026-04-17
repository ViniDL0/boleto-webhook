from fastapi import FastAPI, Request
import requests

from auth_bling import obter_access_token

app = FastAPI()

# 🔑 TOKEN DIGISAC
DIGISAC_TOKEN = "06d78a983534160bedcad20f2256ac715dcf8257"

# 🧠 Memória
usuarios = {}

# 📩 Enviar mensagem
def enviar_mensagem(contact_id, texto):
    url = "https://api.digisac.co/v1/messages"

    headers = {
        "Authorization": f"Bearer {DIGISAC_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "contactId": contact_id,
        "type": "text",
        "text": texto
    }

    requests.post(url, json=body, headers=headers)


# 📄 Enviar boleto (PDF/link)
def enviar_documento(contact_id, url_pdf):
    url = "https://api.digisac.co/v1/messages"

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

    requests.post(url, json=body, headers=headers)


# 🔎 Buscar boletos por CPF (USANDO SEU AUTH)
def buscar_boletos_por_cpf(cpf):
    token = obter_access_token()

    url = "https://api.bling.com.br/Api/v3/contas/receber"

    headers = {
        "Authorization": f"Bearer {token}"
    }

    params = {
        "limite": 100,
        "situacoes[]": [1, 3]  # aberto + atrasado
    }

    response = requests.get(url, headers=headers, params=params)

    print("Resposta Bling:", response.text)

    if response.status_code != 200:
        return []

    dados = response.json().get("data", [])

    cpf = cpf.replace(".", "").replace("-", "").replace("/", "")

    boletos = []

    for b in dados:
        contato = b.get("contato", {})
        doc = str(contato.get("numeroDocumento", "")).replace(".", "").replace("-", "").replace("/", "")

        if cpf in doc:
            boletos.append(b)

    return boletos


# 🧠 Agrupar por pedido
def agrupar_boletos_por_pedido(lista):
    pedidos = {}

    for b in lista:
        numero = str(b.get("numeroDocumento", "Sem pedido"))

        if "/" in numero:
            pedido = numero.split("/")[0]
        else:
            pedido = numero

        if pedido not in pedidos:
            pedidos[pedido] = []

        pedidos[pedido].append(b)

    return pedidos


@app.get("/")
def home():
    return {"status": "online"}


@app.post("/webhook/digisac")
async def webhook(request: Request):
    body = await request.json()
    print("Webhook recebido:", body)

    data = body.get("data", {})
    contact_id = data.get("contactId")
    mensagem = data.get("text", "").lower()
    is_from_me = data.get("isFromMe", True)

    if is_from_me:
        return {"status": "ok"}

    estado = usuarios.get(contact_id, {}).get("estado")
    comando = data.get("command") or mensagem

    # 🔹 INÍCIO
    if comando == "SEGUNDA_VIA":
        usuarios[contact_id] = {"estado": "AGUARDANDO_CPF"}

        enviar_mensagem(contact_id, "Digite seu CPF ou CNPJ para localizar seus boletos")
        return {"status": "ok"}

    # 🔹 CPF
    if estado == "AGUARDANDO_CPF":
        cpf = mensagem

        enviar_mensagem(contact_id, "Buscando seus boletos... 🔍")

        boletos = buscar_boletos_por_cpf(cpf)

        if not boletos:
            enviar_mensagem(contact_id, "Nenhum boleto encontrado ❌")
            return {"status": "ok"}

        pedidos = agrupar_boletos_por_pedido(boletos)

        mapa = {}
        texto = "Encontrei os seguintes pedidos:\n\n"

        for i, (pedido, lista) in enumerate(pedidos.items(), start=1):
            texto += f"{i}️⃣ Pedido {pedido} ({len(lista)} parcelas)\n"
            mapa[str(i)] = pedido

        texto += "\nDigite o número do pedido desejado"

        usuarios[contact_id] = {
            "estado": "AGUARDANDO_PEDIDO",
            "pedidos": pedidos,
            "mapa_pedidos": mapa
        }

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    # 🔹 ESCOLHER PEDIDO
    if estado == "AGUARDANDO_PEDIDO":
        mapa = usuarios[contact_id]["mapa_pedidos"]
        pedido = mapa.get(mensagem)

        if not pedido:
            enviar_mensagem(contact_id, "Opção inválida")
            return {"status": "ok"}

        boletos = usuarios[contact_id]["pedidos"][pedido]

        texto = "Boletos disponíveis:\n\n"
        mapa_boletos = {}

        for i, b in enumerate(boletos, start=1):
            valor = b.get("valor")
            venc = b.get("vencimento")

            texto += f"{i}️⃣ R$ {valor} - vence {venc}\n"
            mapa_boletos[str(i)] = b

        texto += "\nDigite o número ou TODOS"

        usuarios[contact_id]["estado"] = "AGUARDANDO_BOLETO"
        usuarios[contact_id]["mapa_boletos"] = mapa_boletos

        enviar_mensagem(contact_id, texto)
        return {"status": "ok"}

    # 🔹 ESCOLHER BOLETO
    if estado == "AGUARDANDO_BOLETO":
        mapa = usuarios[contact_id]["mapa_boletos"]

        if mensagem == "todos":
            for b in mapa.values():
                link = b.get("linkBoleto") or b.get("url")
                if link:
                    enviar_documento(contact_id, link)

        elif mensagem in mapa:
            b = mapa[mensagem]
            link = b.get("linkBoleto") or b.get("url")

            if link:
                enviar_documento(contact_id, link)
            else:
                enviar_mensagem(contact_id, "Não encontrei o link do boleto")

        else:
            enviar_mensagem(contact_id, "Opção inválida")
            return {"status": "ok"}

        enviar_mensagem(contact_id, "Posso encerrar o atendimento?\n\n1️⃣ Sim\n2️⃣ Não")
        usuarios[contact_id]["estado"] = "FINALIZANDO"

        return {"status": "ok"}

    # 🔹 FINALIZAÇÃO
    if estado == "FINALIZANDO":

        if mensagem == "1":
            enviar_mensagem(contact_id, "Atendimento encerrado ✅")
            usuarios.pop(contact_id, None)

        elif mensagem == "2":
            enviar_mensagem(contact_id, "Vou te transferir para o financeiro 👨‍💼")
            usuarios.pop(contact_id, None)

        else:
            enviar_mensagem(contact_id, "Digite 1 ou 2")

        return {"status": "ok"}

    return {"status": "ok"}