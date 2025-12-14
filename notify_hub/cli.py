import os
import requests
from dotenv import load_dotenv

def post(url: str, payload: dict):
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()

def main():
    load_dotenv()
    slack = os.getenv("SLACK_WEBHOOK_URL")
    discord = os.getenv("DISCORD_WEBHOOK_URL")

    title = "notify-hub テスト"
    body = "まずは通知だけ飛ばす（監視は次）"

    if slack:
        post(slack, {"text": f"*{title}*\n{body}"})
        print("✅ Slack OK")

    if discord:
        post(discord, {"content": f"**{title}**\n{body}"})
        print("✅ Discord OK")

    if not slack and not discord:
        print("⚠️ .envにWebhookが入ってない")

if __name__ == "__main__":
    main()
