"""Run this once to create the Telethon session file."""
import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

with TelegramClient("session", API_ID, API_HASH) as client:
    me = client.get_me()
    print(f"\nLogged in as: {me.first_name} (@{me.username})")
    print("Session saved. You can now run main.py")
