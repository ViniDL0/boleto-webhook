import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("BLING_CLIENT_ID")
CLIENT_SECRET = os.getenv("BLING_CLIENT_SECRET")
REDIRECT_URI = os.getenv("BLING_REDIRECT_URI", "http://localhost:8080/callback")

BLING_ACCESS_TOKEN = os.getenv("BLING_ACCESS_TOKEN")
BLING_REFRESH_TOKEN = os.getenv("BLING_REFRESH_TOKEN")

BLING_BASE_URL = "https://api.bling.com.br/Api/v3"
TOKEN_URL = "https://api.bling.com.br/Api/v3/oauth/token"

DIGISAC_TOKEN = os.getenv("DIGISAC_TOKEN")
DIGISAC_BASE_URL = os.getenv("DIGISAC_BASE_URL")