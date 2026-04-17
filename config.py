import os
import sys
from dotenv import load_dotenv

def caminho(arquivo):
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, arquivo)

load_dotenv(caminho(".env"))

CLIENT_ID     = os.getenv("BLING_CLIENT_ID")
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET")
REDIRECT_URI  = os.getenv("BLING_REDIRECT_URI", "http://localhost:8080/callback")

CEDRUS_API_KEY  = os.getenv("CEDRUS_API_KEY")
CEDRUS_BASE_URL = os.getenv("CEDRUS_BASE_URL", "https://api.sistemadecobranca.com.br:3001/v1")

TOKEN_FILE     = caminho("bling_token.json")
BLING_BASE_URL = "https://api.bling.com.br/Api/v3"
AUTH_URL       = "https://www.bling.com.br/Api/v3/oauth/authorize"
TOKEN_URL      = "https://api.bling.com.br/Api/v3/oauth/token"