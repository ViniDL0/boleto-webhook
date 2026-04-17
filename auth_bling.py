import time
import requests

from config import (
    CLIENT_ID,
    CLIENT_SECRET,
    BLING_ACCESS_TOKEN,
    BLING_REFRESH_TOKEN,
    TOKEN_URL,
)

_token_cache = {
    "access_token": BLING_ACCESS_TOKEN,
    "refresh_token": BLING_REFRESH_TOKEN,
    "expires_at": 0,
}


def _validar_config():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise Exception("BLING_CLIENT_ID ou BLING_CLIENT_SECRET não configurados.")
    if not _token_cache["refresh_token"]:
        raise Exception("BLING_REFRESH_TOKEN não configurado.")


def renovar_token():
    _validar_config()

    response = requests.post(
        TOKEN_URL,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={
            "grant_type": "refresh_token",
            "refresh_token": _token_cache["refresh_token"],
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise Exception(f"Erro ao renovar token do Bling: {response.status_code} - {response.text}")

    token = response.json()

    _token_cache["access_token"] = token["access_token"]
    _token_cache["refresh_token"] = token.get("refresh_token", _token_cache["refresh_token"])
    _token_cache["expires_at"] = int(time.time()) + int(token.get("expires_in", 21600)) - 60

    return _token_cache["access_token"]


def obter_access_token():
    if _token_cache["access_token"]:
        return _token_cache["access_token"]

    return renovar_token()


def forcar_refresh():
    return renovar_token()