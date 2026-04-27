import os
import sys
from dotenv import load_dotenv


def caminho(arquivo):
    """Retorna o caminho absoluto de um arquivo na pasta do projeto."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, arquivo)


load_dotenv(caminho(".env"))

# =====================================================
# BLING
# =====================================================

CLIENT_ID = os.getenv("BLING_CLIENT_ID")
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET")
REDIRECT_URI = os.getenv("BLING_REDIRECT_URI", "http://localhost:8080/callback")

# Tokens iniciais vindos do .env/Railway.
# Depois da primeira renovação, o token novo será salvo em bling_token.json.
BLING_ACCESS_TOKEN = os.getenv("BLING_ACCESS_TOKEN")
BLING_REFRESH_TOKEN = os.getenv("BLING_REFRESH_TOKEN")

TOKEN_FILE = caminho("bling_token.json")
BLING_BASE_URL = "https://api.bling.com.br/Api/v3"
AUTH_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"

# =====================================================
# DIGISAC
# =====================================================

DIGISAC_TOKEN = os.getenv("DIGISAC_TOKEN")
DIGISAC_BASE_URL = os.getenv("DIGISAC_BASE_URL")

DIGISAC_DEPARTMENT_ID_FINANCEIRO = os.getenv(
    "DIGISAC_DEPARTMENT_ID_FINANCEIRO",
    "cda4287a-d6d8-4092-a83a-c379295488d4"
)

DIGISAC_USER_ID_FINANCEIRO = os.getenv("DIGISAC_USER_ID_FINANCEIRO", "")
