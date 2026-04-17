import json
import time
import threading
import webbrowser
import secrets
import requests

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from config import *


def salvar_token(token):
    token["obtido_em"] = int(time.time())
    token["expires_at"] = token["obtido_em"] + token["expires_in"] - 60

    with open(TOKEN_FILE, "w") as f:
        json.dump(token, f, indent=4)


def carregar_token():
    try:
        with open(TOKEN_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def token_valido(token):
    if not token:
        return False
    return token.get("expires_at", 0) > time.time()


def renovar_token(refresh_token):
    response = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
    )

    if response.status_code != 200:
        raise Exception(f"Erro ao renovar token: {response.text}")

    token = response.json()
    salvar_token(token)

    return token["access_token"]


def _autenticar():
    state = secrets.token_hex(16)

    class CallbackHandler(BaseHTTPRequestHandler):

        def log_message(self, format, *args):
            pass

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)

            if "error" in query:
                print("Erro OAuth:", query)
                self.send_response(400)
                self.end_headers()
                return

            if "code" in query:
                code = query["code"][0]

                response = requests.post(
                    TOKEN_URL,
                    auth=(CLIENT_ID, CLIENT_SECRET),
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT_URI
                    }
                )

                if response.status_code == 200:
                    salvar_token(response.json())
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(
                        "Autenticado com sucesso! Pode fechar esta aba.".encode()
                    )
                    print("Autenticacao concluida com sucesso!\n")
                else:
                    print("Erro ao obter token:", response.text)
                    self.send_response(400)
                    self.end_headers()

                threading.Thread(target=self.server.shutdown).start()

            else:
                self.send_response(400)
                self.end_headers()

    url_login = (
        f"{AUTH_URL}"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&state={state}"
    )

    print("Abrindo navegador para autenticacao...")
    webbrowser.open(url_login)

    server = HTTPServer(("localhost", 8080), CallbackHandler)
    server.serve_forever()


def obter_access_token():
    token = carregar_token()

    if token and token_valido(token):
        return token["access_token"]

    if token and not token_valido(token):
        try:
            return renovar_token(token["refresh_token"])
        except Exception:
            pass

    # Token ausente ou renovacao falhou — inicia fluxo de login
    print("Token nao encontrado ou expirado. Iniciando autenticacao...\n")
    _autenticar()

    token = carregar_token()
    if token:
        return token["access_token"]

    raise Exception("Falha na autenticacao. Tente novamente.")