from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def home():
    return {"status": "online"}

@app.post("/webhook/digisac")
async def webhook(request: Request):
    data = await request.json()
    print("Webhook recebido:", data)

    return {"status": "ok"}