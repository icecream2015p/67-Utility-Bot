import ast
import json
import math
import operator
import os
import unicodedata
from pathlib import Path
from threading import Lock, Thread

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


# ---------------------------------------------------------------
# 2. メモの保存先（ユーザーごとにJSONファイルへ保存）
# ---------------------------------------------------------------
MEMO_FILE = Path(os.environ.get('MEMO_FILE', Path(__file__).with_name('memos.json')))


class MemoStore:
  """ユーザーIDごとにメモのリストをJSONファイルで管理するクラス。"""

  def __init__(self, path: Path):
    self.path = Path(path)
    self._lock = Lock()
    self._data = self._load()

  def _load(self) -> dict[str, list[str]]:
    try:
      with self.path.open(encoding='utf-8') as f:
        data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
      return {}
    if not isinstance(data, dict):
      return {}
    cleaned: dict[str, list[str]] = {}
    for key, value in data.items():
      if isinstance(value, list):
        cleaned[str(key)] = [str(item) for item in value]
    return cleaned

  def _save(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_name(self.path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
      json.dump(self._data, f, ensure_ascii=False, indent=2)
    tmp.replace(self.path)

  def get(self, user_id: int) -> list[str]:
    with self._lock:
      return list(self._data.get(str(user_id), []))

  def add(self, user_id: int, text: str, position: int | None = None) -> int:
    """メモを追加し、追加した位置（1始まり）を返す。"""
    text = text.strip()
    if not text:
      raise MemoError('メモの内容が空です。')
    with self._lock:
      memos = self._data.setdefault(str(user_id), [])
      if position is None:
        memos.append(text)
        index = len(memos) - 1
      else:
        # 1 〜 件数+1 の範囲で挿入できる（件数+1 = 一番下に追加）
        if position < 1 or position > len(memos) + 1:
          raise MemoError(
              f'追加できる位置は 1 〜 {len(memos) + 1} です（現在 {len(memos)} 件）。'
          )
        index = position - 1
        memos.insert(index, text)
      self._save()
      return index + 1

  def delete(self, user_id: int, position: int) -> str:
    """指定した位置（1始まり）のメモを削除し、削除した内容を返す。"""
    with self._lock:
      memos = self._data.get(str(user_id), [])
      if not memos:
        raise MemoError('メモが1件も登録されていません。')
      if position < 1 or position > len(memos):
        raise MemoError(
            f'削除できる番号は 1 〜 {len(memos)} です（現在 {len(memos)} 件）。'
        )
      removed = memos.pop(position - 1)
      self._data[str(user_id)] = memos
      self._save()
      return removed


class MemoError(Exception):
  """メモ操作のユーザー向けエラー。"""


memo_store = MemoStore(MEMO_FILE)


# ---------------------------------------------------------------
# 3. 電卓の計算処理（安全に式を評価する）
# ---------------------------------------------------------------
_MAX_EXPRESSION_LENGTH = 200
_MAX_POW_EXPONENT = 1000


class CalcError(Exception):
  """電卓のユーザー向けエラー。"""


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def normalize_expression(expression: str) -> str:
  """全角の数字・記号や電卓用の記号を、Pythonで計算できる形に変換する。"""
  # 全角数字・全角英字を半角に、全角記号を半角記号に変換する
  expression = unicodedata.normalize('NFKC', expression)
  table = str.maketrans({
      '×': '*',
      '÷': '/',
      '−': '-',
      'ー': '-',
      '＊': '*',
      '／': '/',
      '＋': '+',
      '　': ' ',
  })
  # 桁区切りのカンマは無視する（1,000 -> 1000）
  return expression.translate(table).replace(',', '').replace('、', '').strip()


def format_number(value: float | int) -> str:
  if isinstance(value, bool):
    raise CalcError('計算できませんでした。')
  if isinstance(value, int):
    return str(value)
  if math.isnan(value):
    raise CalcError('計算結果を求められませんでした。')
  if math.isinf(value):
    raise CalcError('計算結果が大きすぎます。')
  if value.is_integer() and abs(value) < 1e18:
    return str(int(value))
  formatted = f'{value:.12g}'
  if 'e' in formatted:
    formatted = f'{value:.10f}'.rstrip('0').rstrip('.')
  return formatted


def safe_eval(expression: str) -> float | int:
  """数式を安全に評価する。許可した構文（数値・四則演算・べき乗・括弧）のみ受け付ける。"""
  normalized = normalize_expression(expression)
  if not normalized:
    raise CalcError('計算式が空です。')
  if len(normalized) > _MAX_EXPRESSION_LENGTH:
    raise CalcError(f'計算式は{_MAX_EXPRESSION_LENGTH}文字以内にしてください。')

  try:
    tree = ast.parse(normalized, mode='eval')
  except SyntaxError:
    raise CalcError('計算式を解釈できませんでした。`(1 + 2) * 3` のように入力してください。')

  node_count = sum(1 for _ in ast.walk(tree))
  if node_count > 100:
    raise CalcError('計算式が複雑すぎます。')

  def evaluate(node):
    if isinstance(node, ast.Expression):
      return evaluate(node.body)
    if isinstance(node, ast.Constant):
      if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
      raise CalcError('数値以外は使えません。')
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
      left = evaluate(node.left)
      right = evaluate(node.right)
      if isinstance(node.op, ast.Pow):
        if abs(right) > _MAX_POW_EXPONENT:
          raise CalcError(f'べき乗の指数は{_MAX_POW_EXPONENT}以下にしてください。')
        if abs(left) > 1e6 and right > 100:
          raise CalcError('計算結果が大きすぎます。')
      try:
        return _BIN_OPS[type(node.op)](left, right)
      except ZeroDivisionError:
        raise CalcError('0で割ることはできません。')
      except OverflowError:
        raise CalcError('計算結果が大きすぎます。')
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
      return _UNARY_OPS[type(node.op)](evaluate(node.operand))
    raise CalcError('使えない記号が含まれています（数値・+ - * / % ** ( ) のみ）。')

  try:
    result = evaluate(tree)
  except CalcError:
    raise
  except (RecursionError, MemoryError, OverflowError):
    raise CalcError('計算結果が大きすぎます。')

  # 無限大になった場合はエラーとして扱う
  if isinstance(result, float) and not math.isfinite(result):
    raise CalcError('計算結果が大きすぎます。')
  return result


# ---------------------------------------------------------------
# 4. 電卓のボタンUI
# ---------------------------------------------------------------
class CalculatorView(discord.ui.View):
  """ボタンで操作できる電卓。"""

  def __init__(self, expression: str = ''):
    super().__init__(timeout=600)
    self.expression = expression
    layout = [
        ('C', 'C', discord.ButtonStyle.danger, 0),
        ('⌫', 'backspace', discord.ButtonStyle.secondary, 0),
        ('(', '(', discord.ButtonStyle.secondary, 0),
        (')', ')', discord.ButtonStyle.secondary, 0),
        ('÷', '÷', discord.ButtonStyle.primary, 0),
        ('7', '7', discord.ButtonStyle.secondary, 1),
        ('8', '8', discord.ButtonStyle.secondary, 1),
        ('9', '9', discord.ButtonStyle.secondary, 1),
        ('×', '×', discord.ButtonStyle.primary, 1),
        ('**', '**', discord.ButtonStyle.secondary, 1),
        ('4', '4', discord.ButtonStyle.secondary, 2),
        ('5', '5', discord.ButtonStyle.secondary, 2),
        ('6', '6', discord.ButtonStyle.secondary, 2),
        ('-', '-', discord.ButtonStyle.primary, 2),
        ('%', '%', discord.ButtonStyle.secondary, 2),
        ('1', '1', discord.ButtonStyle.secondary, 3),
        ('2', '2', discord.ButtonStyle.secondary, 3),
        ('3', '3', discord.ButtonStyle.secondary, 3),
        ('+', '+', discord.ButtonStyle.primary, 3),
        ('=', '=', discord.ButtonStyle.success, 3),
        ('0', '0', discord.ButtonStyle.secondary, 4),
        ('.', '.', discord.ButtonStyle.secondary, 4),
    ]
    for label, action, style, row in layout:
      self.add_item(self._make_button(label, action, style, row))

    clear_button = discord.ui.Button(
        label='結果をクリア', style=discord.ButtonStyle.secondary, row=4
    )
    clear_button.callback = self._make_callback('C')
    self.add_item(clear_button)

  def _make_button(self, label, action, style, row):
    button = discord.ui.Button(label=label, style=style, row=row)
    button.callback = self._make_callback(action)
    return button

  def _make_callback(self, action):
    async def callback(interaction: discord.Interaction):
      if action == 'C':
        self.expression = ''
      elif action == 'backspace':
        self.expression = self.expression[:-1]
      elif action == '=':
        if not self.expression:
          await interaction.response.send_message(
              '計算式がまだ入力されていません。', ephemeral=True
          )
          return
        try:
          self.expression = format_number(safe_eval(self.expression))
        except CalcError as e:
          await interaction.response.send_message(str(e), ephemeral=True)
          return
      else:
        if len(self.expression) >= _MAX_EXPRESSION_LENGTH:
          await interaction.response.send_message(
              f'計算式は{_MAX_EXPRESSION_LENGTH}文字以内にしてください。',
              ephemeral=True,
          )
          return
        self.expression += action
      await interaction.response.edit_message(
          content=self.render(), view=self
      )

    return callback

  def render(self) -> str:
    shown = self.expression or '0'
    if len(shown) > 300:
      shown = '…' + shown[-300:]
    lines = ['### 🧮 電卓', '```', shown, '```']
    # 演算子が含まれているときだけ、途中経過の計算結果を表示する
    if any(op in self.expression for op in '+-*/%'):
      try:
        lines.append(f'結果: **{format_number(safe_eval(self.expression))}**')
      except CalcError:
        pass
    lines.append('-# ボタンで入力できます。式を直接渡すときは `/dentaku 式:1+2*3`')
    return '\n'.join(lines)

  async def on_timeout(self):
    for item in self.children:
      item.disabled = True
    # 一定時間操作がなければ、ボタンを押せないようにする
    if self.message is not None:
      try:
        await self.message.edit(
            content='⌛ 電卓の有効期限が切れました。もう一度 `/dentaku` を実行してください。',
            view=self,
        )
      except discord.HTTPException:
        pass


# ---------------------------------------------------------------
# 5. ユーザーインストール対応のBotクラス
# ---------------------------------------------------------------
class MyBot(discord.Client):

  def __init__(self):
    # User Installの場合、特別なIntentsは基本的に不要です（デフォルトでOK）
    super().__init__(intents=discord.Intents.default())
    # 個人用Botとして、サーバー／DM／ユーザーインストールのどこからでも使えるようにする
    self.tree = app_commands.CommandTree(
        self,
        allowed_contexts=app_commands.AppCommandContext(
            guild=True, dm_channel=True, private_channel=True
        ),
        allowed_installs=app_commands.AppInstallationType(guild=True, user=True),
    )

  async def setup_hook(self):
    # Discordにスラッシュコマンドを同期する
    await self.tree.sync()
    print("スラッシュコマンドの同期が完了しました")

  async def on_ready(self):
    print(f"ログインしました: {client.user}")


client = MyBot()

NO_MENTIONS = discord.AllowedMentions.none()
_MARKDOWN_SPECIAL = r'\`*_~|>[]()#'
MAX_MEMO_LENGTH = 1000
AUTOCOMPLETE_LIMIT = 25


def escape_markdown(text: str) -> str:
  """メモの内容がMarkdownとして解釈されないようにエスケープする。"""
  return ''.join('\\' + ch if ch in _MARKDOWN_SPECIAL else ch for ch in text)


def shorten(text: str, limit: int = 200) -> str:
  text = text.replace('\r', ' ').replace('\n', ' ⏎ ')
  if len(text) > limit:
    text = text[:limit] + '…'
  return text


# ---------------------------------------------------------------
# 6. スラッシュコマンドの定義
# ---------------------------------------------------------------
@client.tree.command(
    name='dentaku',
    description='電卓機能（ボタン式の電卓を表示、または式を渡して計算）',
)
@app_commands.describe(式='計算したい式を入力するとその場で計算します（例: (1 + 2) * 3）。省略するとボタン式の電卓を表示')
async def dentaku(interaction: discord.Interaction, 式: str | None = None):
  # 式が指定されたら、その場で計算して結果を返す
  if 式 is not None:
    try:
      result = format_number(safe_eval(式))
    except CalcError as e:
      await interaction.response.send_message(
          f'⚠️ {e}', ephemeral=True, allowed_mentions=NO_MENTIONS
      )
      return
    await interaction.response.send_message(
        f'🧮 `{式.strip()[:200]}` = **{result}**',
        allowed_mentions=NO_MENTIONS,
    )
    return

  # 式がなければ、ボタンで操作できる電卓を表示する
  view = CalculatorView()
  await interaction.response.send_message(
      content=view.render(), view=view, allowed_mentions=NO_MENTIONS
  )


memo_group = app_commands.Group(name='memo', description='メモの追加・一覧・削除')


def build_memo_lines(memos: list[str]) -> list[str]:
  """メモ一覧を「1. 内容」形式の行リストにする。"""
  return [
      f'**{index}.** {escape_markdown(shorten(memo))}'
      for index, memo in enumerate(memos, start=1)
  ]


@memo_group.command(name='list', description='登録されているメモをすべて表示します')
async def memo_list(interaction: discord.Interaction):
  memos = memo_store.get(interaction.user.id)
  if not memos:
    await interaction.response.send_message(
        '📝 メモはまだ登録されていません。`/memo add` で追加できます。',
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
    return

  lines = build_memo_lines(memos)
  header = f'📝 **メモ一覧**（全{len(memos)}件）\n'
  # Discordの1メッセージ2000文字制限に収まるように分割して送信する
  chunks: list[str] = []
  current = header
  for line in lines:
    if len(current) + len(line) + 1 > 1900:
      chunks.append(current.rstrip('\n'))
      current = ''
    current += line + '\n'
  if current.strip():
    chunks.append(current.rstrip('\n'))

  await interaction.response.send_message(
      chunks[0], allowed_mentions=NO_MENTIONS
  )
  for chunk in chunks[1:]:
    await interaction.followup.send(chunk, allowed_mentions=NO_MENTIONS)


@memo_group.command(
    name='add', description='メモを追加します（位置を省略すると一番下に追加）'
)
@app_commands.describe(
    内容='追加するメモの内容',
    位置='上から何番目に入れるか（省略すると一番下）',
)
async def memo_add(
    interaction: discord.Interaction,
    内容: str,
    位置: app_commands.Range[int, 1] | None = None,
):
  content = 内容.strip()
  if len(content) > MAX_MEMO_LENGTH:
    await interaction.response.send_message(
        f'⚠️ メモは{MAX_MEMO_LENGTH}文字以内で入力してください（現在{len(content)}文字）。',
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
    return

  try:
    position = memo_store.add(interaction.user.id, content, 位置)
  except MemoError as e:
    await interaction.response.send_message(
        f'⚠️ {e}', ephemeral=True, allowed_mentions=NO_MENTIONS
    )
    return

  total = len(memo_store.get(interaction.user.id))
  quoted = escape_markdown(shorten(content))
  if 位置 is None:
    message = f'✅ メモを追加しました（一番下 / 全{total}件）\n> {quoted}'
  else:
    message = f'✅ {position}番目にメモを追加しました（全{total}件）\n> {quoted}'

  await interaction.response.send_message(message, allowed_mentions=NO_MENTIONS)


@memo_group.command(name='delete', description='指定した番号のメモを削除します')
@app_commands.describe(番号='削除するメモの番号（上から何番目か）。/memo list で確認できます')
async def memo_delete(
    interaction: discord.Interaction, 番号: app_commands.Range[int, 1]
):
  try:
    removed = memo_store.delete(interaction.user.id, 番号)
  except MemoError as e:
    await interaction.response.send_message(
        f'⚠️ {e}', ephemeral=True, allowed_mentions=NO_MENTIONS
    )
    return

  total = len(memo_store.get(interaction.user.id))
  await interaction.response.send_message(
      f'🗑️ {番号}番目のメモを削除しました（残り{total}件）\n'
      f'> {escape_markdown(shorten(removed))}',
      allowed_mentions=NO_MENTIONS,
  )


@memo_delete.autocomplete('番号')
async def memo_delete_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
  """削除する番号を選びやすくするため、現在のメモを候補として表示する。"""
  memos = memo_store.get(interaction.user.id)
  current = (current or '').strip()
  choices: list[app_commands.Choice[int]] = []
  for index, memo in enumerate(memos, start=1):
    label = f'{index}. {shorten(memo, 80)}'
    if current and current not in label and not str(index).startswith(current):
      continue
    choices.append(app_commands.Choice(name=label[:100], value=index))
    if len(choices) >= AUTOCOMPLETE_LIMIT:
      break
  return choices


@client.tree.command(name='help', description='各コマンドの説明を表示します')
async def help_command(interaction: discord.Interaction):
  embed = discord.Embed(
      title='🤖 コマンド一覧',
      description='このBotで使えるスラッシュコマンドの説明です。',
      color=discord.Color.blurple(),
  )
  embed.add_field(
      name='🧮 `/dentaku [式]`',
      value=(
          '電卓機能です。\n'
          '・式を省略 → ボタン式の電卓を表示（`=` で計算、`C` でクリア）\n'
          '・式を指定 → その場で計算して結果を表示\n'
          '使える演算: `+` `-` `*` `/` `%` `**` `( )` と括弧\n'
          '全角の `×` `÷` `＋` `（）` やカンマ区切り（`1,000`）も使えます。\n'
          '例: `/dentaku 式:(1 + 2) * 3`'
      ),
      inline=False,
  )
  embed.add_field(
      name='📝 `/memo list`',
      value='登録されているメモをすべて表示します。',
      inline=False,
  )
  embed.add_field(
      name='➕ `/memo add 内容: [位置:]`',
      value=(
          'メモを追加します。\n'
          '`位置` を指定すると「上から何番目」に挿入します。\n'
          '省略すると一番下に追加されます。\n'
          '例: `/memo add 内容:牛乳を買う`\n'
          '例: `/memo add 内容:重要 位置:1`'
      ),
      inline=False,
  )
  embed.add_field(
      name='🗑️ `/memo delete 番号:`',
      value=(
          '指定した番号（上から何番目か）のメモを削除します。\n'
          '番号は `/memo list` で確認できます。\n'
          '例: `/memo delete 番号:2`'
      ),
      inline=False,
  )
  embed.add_field(
      name='❓ `/help`',
      value='このコマンド一覧を表示します。',
      inline=False,
  )
  embed.set_footer(text='メモはユーザーごとに保存されます')
  await interaction.response.send_message(
      embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
  )


client.tree.add_command(memo_group)


# 7. Webサーバーを起動してからBotを動かす
if __name__ == '__main__':
  keep_alive()
  client.run(os.environ.get('DISCORD_BOT_TOKEN'))
