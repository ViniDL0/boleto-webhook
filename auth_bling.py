import json
import time
import threading
import requests

from config import (
    CLIENT_ID,
    CLIENT_SECRET,
    TOKEN_URL,
    TOKEN_FILE,
    BLING_ACCESS_TOKEN,
    BLING_REFRESH_TOKEN,
)

_token_lock = threading.Lock()

JWT_HEADER = {"enable-jwt": "1"}


def salvar_token(token):
    expires_in = int(token.get("expires_in", 3600) or 3600)
    agora = int(time.time())

    token["obtido_em"] = agora
    token["expires_at"] = agora + expires_in - 120

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=4, ensure_ascii=False)


def carregar_token():
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    if BLING_ACCESS_TOKEN or BLING_REFRESH_TOKEN:
        token = {
            "access_token": BLING_ACCESS_TOKEN,
            "refresh_token": BLING_REFRESH_TOKEN,
            "token_type": "Bearer",
            "expires_in": 0,
            "obtido_em": 0,
            "expires_at": 0,
        }

        try:
            salvar_token(token)
        except Exception as e:
            print("ERRO_SALVAR_TOKEN_INICIAL_BLING:", e)

        return token

    return None


def token_valido(token):
    if not token:
        return False

    access_token = token.get("access_token")
    expires_at = float(token.get("expires_at", 0) or 0)

    return bool(access_token) and expires_at > time.time()


def renovar_token(refresh_token):
    if not refresh_token:
        raise Exception("Refresh token do Bling não encontrado.")

    response = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "1.0",
            "enable-jwt": "1",
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )

    print("RENOVAR_TOKEN_BLING:", response.status_code, response.text[:500])

    if response.status_code != 200:
        raise Exception(f"Erro ao renovar token do Bling: {response.text}")

    token = response.json()

    if not token.get("access_token"):
        raise Exception(f"Bling não retornou access_token: {token}")

    salvar_token(token)

    return token["access_token"]


def obter_access_token():
    with _token_lock:
        token = carregar_token()

        if token_valido(token):
            return token["access_token"]

        refresh_token = None

        if token:
            refresh_token = token.get("refresh_token")

        if not refresh_token:
            refresh_token = BLING_REFRESH_TOKEN

        return renovar_token(refresh_token)


def forcar_refresh():
    with _token_lock:
        token = carregar_token()

        refresh_token = None

        if token:
            refresh_token = token.get("refresh_token")

        if not refresh_token:
            refresh_token = BLING_REFRESH_TOKEN

        return renovar_token(refresh_token)


def bling_headers():
    token = obter_access_token()

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "enable-jwt": "1",
    }