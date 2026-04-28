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


def salvar_token(token):
    """Salva access_token/refresh_token em arquivo local."""
    expires_in = int(token.get("expires_in", 3600) or 3600)
    token["obtido_em"] = int(time.time())
    token["expires_at"] = token["obtido_em"] + expires_in - 60

    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(token, f, indent=4, ensure_ascii=False)


def carregar_token():
    """Carrega token do arquivo. Se não existir, cria token inicial a partir do .env/Railway."""
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

        # Salva o token inicial para o próximo uso.
        try:
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump(token, f, indent=4, ensure_ascii=False)
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
    """Renova token no Bling usando refresh_token e salva o novo retorno."""
    if not refresh_token:
        raise Exception("Refresh token do Bling não encontrado.")

    response = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        headers={
            "enable-jwt": "1"
        },
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )

    print("RENOVAR_TOKEN_BLING:", response.status_code, response.text)

    if response.status_code != 200:
        raise Exception(f"Erro ao renovar token do Bling: {response.text}")

    token = response.json()
    salvar_token(token)
    return token["access_token"]


def obter_access_token():
    """Retorna access_token válido. Se estiver vencido, renova automaticamente."""
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
    """Força renovação, usado quando uma chamada ao Bling retorna 401."""
    with _token_lock:
        token = carregar_token()
        refresh_token = None

        if token:
            refresh_token = token.get("refresh_token")

        if not refresh_token:
            refresh_token = BLING_REFRESH_TOKEN

        return renovar_token(refresh_token)
