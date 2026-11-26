import os
import threading
import discord
import requests

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "http://n8n_local:5678/webhook/redteam")
# ALLOWED_CHANNELS should be set via DISCORD_ALLOWED_CHANNELS env var (comma-separated channel IDs)
# Example: DISCORD_ALLOWED_CHANNELS=123456789,987654321
ALLOWED_CHANNELS_RAW = os.environ.get("DISCORD_ALLOWED_CHANNELS", "")
ALLOWED_CHANNELS = set(int(c.strip()) for c in ALLOWED_CHANNELS_RAW.split(",") if c.strip().isdigit())

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def forward_to_n8n(payload):
    try:
        # Timeout de 300s porque Guardian + RAG + Analyst tardan ~3.5 min en CPU
        requests.post(N8N_WEBHOOK_URL, json=payload, timeout=300)
    except Exception as e:
        print(f"[Error enviando a n8n]: {e}", flush=True)


@client.event
async def on_ready():
    print(f"Bot puente conectado como {client.user} escuchando en {ALLOWED_CHANNELS}", flush=True)


@client.event
async def on_message(message):
    # Ignorar mensajes de bots (incluido el propio LLM RedTeam) para evitar bucles infinitos
    if message.author.bot:
        return
    if message.channel.id not in ALLOWED_CHANNELS:
        return
    if not message.content.strip():
        return

    payload = {
        "prompt": message.content.strip(),
        "session_id": f"discord-{message.channel.id}-{message.author.id}",
        "discord": {
            "channel_id": str(message.channel.id),
            "message_id": str(message.id)
        }
    }
    print(f"Reenviando mensaje de {message.author}: {payload['prompt'][:60]}...", flush=True)
    # Se ejecuta en un hilo separado para no bloquear el heartbeat de Discord
    threading.Thread(target=forward_to_n8n, args=(payload,), daemon=True).start()


client.run(DISCORD_TOKEN)