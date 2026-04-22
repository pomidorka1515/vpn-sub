import disnake
from disnake.ext import commands
import httpx
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from loggers import Logger

log = Logger("Bot")

# CFG = read-only
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
cfg = Config(
    path=os.path.join(SCRIPT_DIR, 'config.json'),
    indent=4,
    read_only=True,
    strict_schema=False,
    sync_mode="none",
    isolate_commits=False
)

client = commands.InteractionBot(intents=disnake.Intents.default())

api = httpx.AsyncClient(
    base_url=cfg.get('api_base_url', ''),
    headers={"Authorization": cfg.get('api_token', '')},
    timeout=5.0
)
@client.event
async def on_ready():
    log.info(f"Logged in as {client.user} ID {client.user.id}")

@client.slash_command(description="Multiplies the number by a multiplier")
async def multiply(inter, number: int, multiplier: int = 7):
    await inter.response.send_message(number * multiplier)

try:
    client.run(cfg.get('token', ''))
except disnake.LoginFailure:
    log.critical("Token invalid, bot will not start!")