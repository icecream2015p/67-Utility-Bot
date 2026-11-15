import os
from threading import Thread
import discord
from discord import app_commands
from flask import Flask

# 1. 簡易的なWebサーバー（Flask）の準備（Renderのスリープ対策用）
app = Flask('')


@app.route('/')
def home():
  return "I'm alive!"


def run():
  port = int(os.environ.get('PORT', 8080))
  app.run(host='0.0.0.0', port=port)


def keep_alive():
  t = Thread(target=run)
  t.start()


# 2. ユーザーインストール対応のBotクラス
class MyBot(discord.Client):

  def __init__(self):
    # User Installの場合、特別なIntentsは基本的に不要です（デフォルトでOK）
    super().__init__(intents=discord.Intents.default())
    self.tree = app_commands.CommandTree(self)

  async def setup_hook(self):
    # Discordにスラッシュコマンドを同期する
    await self.tree.sync()
    print("スラッシュコマンドの同期が完了しました")

  async def on_ready(self):
    print(f"ログインしました: {client.user}")


client = MyBot()


# 3. どこからでも使えるスラッシュコマンドの定義
@client.tree.command(
    name="hello", description="個人用Botから挨拶を返します"
)
async def hello(interaction: discord.Interaction):
  # スラッシュコマンドへの返信（interaction.response.send_message）
  await interaction.response.send_message("こんにちは！24時間稼働中です！")


# 4. Webサーバーを起動してからBotを動かす
if __name__ == '__main__':
  keep_alive()
  client.run(os.environ.get('DISCORD_BOT_TOKEN'))
