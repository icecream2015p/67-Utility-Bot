import os
from threading import Thread
import discord
from flask import Flask

# 1. 簡易的なWebサーバー（Flask）の準備
app = Flask('')


@app.route('/')
def home():
  return "I'm alive!"


def run():
  # Renderが指定するポート（環境変数 PORT）を使用する
  port = int(os.environ.get('PORT', 8080))
  app.run(host='0.0.0.0', port=port)


def keep_alive():
  t = Thread(target=run)
  t.start()


# 2. Discord Botの処理
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
  print(f'ログインしました: {client.user}')


@client.event
async def on_message(message):
  if message.author == client.user:
    return
  if message.content == '/hello':
    await message.channel.send('こんにちは！24時間稼働中です！')


# 3. Webサーバーを起動してからBotを動かす
if __name__ == '__main__':
  keep_alive()
  client.run(os.environ.get('DISCORD_BOT_TOKEN'))