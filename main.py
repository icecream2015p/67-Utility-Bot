import ast
import json
import math
import operator
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from threading import Lock, Thread

from discord.ext import tasks

try:  # Python 3.9+ の標準ライブラリ（Windows等でタイムゾーンデータが無い場合に備える）
  from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
  ZoneInfo = None

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

  def edit(self, user_id: int, position: int, text: str) -> tuple[str, str]:
    """指定した位置のメモを書き換え、(変更前, 変更後) を返す。"""
    text = text.strip()
    if not text:
      raise MemoError('メモの内容が空です。')
    with self._lock:
      memos = self._data.get(str(user_id), [])
      if not memos:
        raise MemoError('メモが1件も登録されていません。')
      if position < 1 or position > len(memos):
        raise MemoError(
            f'編集できる番号は 1 〜 {len(memos)} です（現在 {len(memos)} 件）。'
        )
      before = memos[position - 1]
      memos[position - 1] = text
      self._save()
      return before, text


class MemoError(Exception):
  """メモ操作のユーザー向けエラー。"""


memo_store = MemoStore(MEMO_FILE)



# ---------------------------------------------------------------
# 3. リマインダーの保存先（ユーザーごとにJSONファイルへ保存）
# ---------------------------------------------------------------
REMINDER_FILE = Path(
    os.environ.get('REMINDER_FILE', Path(__file__).with_name('reminders.json'))
)


class ReminderError(Exception):
  """リマインダー操作のユーザー向けエラー。"""


DEFAULT_TIMEZONE = 'Asia/Tokyo'
MAX_REMINDER_HOURS = 24 * 365  # 1年先まで
JST = timezone(timedelta(hours=9), 'JST')


class ReminderStore:
  """ユーザーごとのリマインダーをJSONファイルで管理するクラス。

  保存形式: {"<user_id>": [{"id": 1, "text": "...", "due": "ISO8601", "done": false}]}
  """

  def __init__(self, path: Path, timezone_name: str = DEFAULT_TIMEZONE):
    self.path = Path(path)
    self._lock = Lock()
    self._data = self._load()
    self.timezone_name = timezone_name
    self._tz = self._resolve_timezone(timezone_name)

  @staticmethod
  def _resolve_timezone(name: str) -> tzinfo:
    if ZoneInfo is not None:
      try:
        return ZoneInfo(name)
      except Exception:  # noqa: BLE001 - タイムゾーンデータが無い環境向け
        pass
    return JST

  @property
  def tz(self) -> tzinfo:
    return self._tz

  def now(self) -> datetime:
    return datetime.now(self._tz)

  def _load(self) -> dict[str, list[dict]]:
    try:
      with self.path.open(encoding='utf-8') as f:
        data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
      return {}
    if not isinstance(data, dict):
      return {}
    cleaned: dict[str, list[dict]] = {}
    for key, value in data.items():
      if not isinstance(value, list):
        continue
      items = []
      for raw in value:
        if not isinstance(raw, dict):
          continue
        text = str(raw.get('text', '')).strip()
        due = raw.get('due')
        if not text or not isinstance(due, str):
          continue
        try:
          due_at = datetime.fromisoformat(due)
        except ValueError:
          continue
        channel_id = raw.get('channel_id')
        if not isinstance(channel_id, int):
          channel_id = None
        items.append({
            'id': int(raw.get('id', 0)) or len(items) + 1,
            'text': text,
            'due': due_at.isoformat(),
            'done': bool(raw.get('done', False)),
            'channel_id': channel_id,
            'attempts': int(raw.get('attempts', 0) or 0),
        })
      cleaned[str(key)] = items
    return cleaned

  def _save(self) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = self.path.with_name(self.path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
      json.dump(self._data, f, ensure_ascii=False, indent=2)
    tmp.replace(self.path)

  def _bucket(self, user_id: int) -> list[dict]:
    return self._data.setdefault(str(user_id), [])

  def add(
      self, user_id: int, text: str, due: datetime, channel_id: int | None = None
  ) -> dict:
    text = text.strip()
    if not text:
      raise ReminderError('リマインダーの内容が空です。')
    if due.tzinfo is None:
      due = due.replace(tzinfo=self._tz)
    now = self.now()
    if due <= now:
      # 「0分後」のようにほぼ現在時刻の指定は、すぐ通知する
      if now - due <= timedelta(seconds=60):
        due = now + timedelta(seconds=1)
      else:
        raise ReminderError(
            '指定した日時はすでに過ぎています（未来の日時を指定してください）。'
        )
    if due - now > timedelta(hours=MAX_REMINDER_HOURS):
      raise ReminderError(
          f'指定できるのは{MAX_REMINDER_HOURS // 24}日先までです。'
      )
    with self._lock:
      items = self._bucket(user_id)
      next_id = max((item['id'] for item in items), default=0) + 1
      item = {
          'id': next_id,
          'text': text,
          'due': due.isoformat(),
          'done': False,
          'channel_id': channel_id,
          'attempts': 0,
      }
      items.append(item)
      self._save()
      return dict(item)

  def list_for_user(self, user_id: int, include_done: bool = False) -> list[dict]:
    with self._lock:
      items = [dict(item) for item in self._data.get(str(user_id), [])]
    if not include_done:
      items = [item for item in items if not item['done']]
    items.sort(key=lambda item: item['due'])
    return items

  def get(self, user_id: int, reminder_id: int) -> dict | None:
    with self._lock:
      for item in self._data.get(str(user_id), []):
        if item['id'] == reminder_id:
          return dict(item)
    return None

  def remove(self, user_id: int, reminder_id: int) -> dict:
    with self._lock:
      items = self._data.get(str(user_id), [])
      for index, item in enumerate(items):
        if item['id'] == reminder_id:
          removed = items.pop(index)
          self._save()
          return dict(removed)
    raise ReminderError(f'ID {reminder_id} のリマインダーは見つかりません。')

  def record_attempt(self, user_id: int, reminder_id: int) -> int:
    """通知を試みた回数を1つ増やし、その回数を返す。"""
    with self._lock:
      for item in self._data.get(str(user_id), []):
        if item['id'] == reminder_id:
          item['attempts'] = int(item.get('attempts', 0)) + 1
          self._save()
          return item['attempts']
    return 0

  def mark_done(self, user_id: int, reminder_id: int) -> dict:
    with self._lock:
      for item in self._data.get(str(user_id), []):
        if item['id'] == reminder_id:
          item['done'] = True
          self._save()
          return dict(item)
    raise ReminderError(f'ID {reminder_id} のリマインダーは見つかりません。')

  def due(self, now: datetime | None = None) -> list[tuple[int, dict]]:
    """通知すべき（期限が来た）リマインダーを (ユーザーID, 内容) で返す。"""
    now = now or self.now()
    ready: list[tuple[int, dict]] = []
    with self._lock:
      for key, items in self._data.items():
        for item in items:
          if item['done']:
            continue
          try:
            due_at = datetime.fromisoformat(item['due'])
          except ValueError:
            continue
          if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=self._tz)
          if due_at <= now:
            ready.append((int(key), dict(item)))
    ready.sort(key=lambda pair: pair[1]['due'])
    return ready


reminder_store = ReminderStore(REMINDER_FILE, os.environ.get('BOT_TIMEZONE', DEFAULT_TIMEZONE))


# ---------------------------------------------------------------
# 4. 日時の入力を解釈する（「10:30」「2026/11/20 10:30」「30分後」など）
# ---------------------------------------------------------------
_UNIT_SECONDS = {
    '秒': 1,
    '分': 60,
    '時間': 3600,
    '時': 3600,
    '日': 86400,
    '週': 7 * 86400,
}

_TIME_SUFFIX = (
    r'(?:\s*(?P<H>\d{1,2})(?:[:時](?P<M>\d{1,2})分?|時))?'
)

_ABSOLUTE_PATTERNS = (
    # 2026/11/20 10:30, 2026-11-20 10:30, 2026年11月20日 10時30分
    re.compile(
        r'^(?P<y>\d{4})[/\-年.](?P<m>\d{1,2})[/\-月.](?P<d>\d{1,2})日?'
        + _TIME_SUFFIX + r'$'
    ),
    # 11/20 10:30, 11-20, 11月20日 10時30分
    re.compile(
        r'^(?P<m>\d{1,2})[/\-月.](?P<d>\d{1,2})日?' + _TIME_SUFFIX + r'$'
    ),
    # 今日 10:30 / 明日 9:00 / 明後日 9時
    re.compile(
        r'^(?P<day>今日|きょう|明日|あした|明後日)' + _TIME_SUFFIX + r'$'
    ),
    # 10:30 / 10時30分 / 10時（今日の時刻。過ぎていれば明日）
    re.compile(r'^(?P<H>\d{1,2})(?:[:時](?P<M>\d{1,2})分?|時)$'),
)

_RELATIVE_PATTERN = re.compile(
    r'^(?:あと|後で)?\s*(?P<num>\d+)\s*(?P<unit>秒|分|時間|時|日|週)(?:後|間後|後で)?$'
)

_DAY_OFFSETS = {'今日': 0, 'きょう': 0, '明日': 1, 'あした': 1, '明後日': 2}


def _build_datetime(
    year: int, month: int, day: int, hour: int, minute: int, tz: tzinfo
) -> datetime:
  try:
    return datetime(year, month, day, hour, minute, tzinfo=tz)
  except ValueError:
    raise ReminderError('存在しない日付です。日付を確認してください。')


def parse_due(text: str, now: datetime) -> datetime:
  """日時表現を解釈して timezone 付きの datetime を返す。"""
  raw = unicodedata.normalize('NFKC', text).strip()
  if not raw:
    raise ReminderError(
        '日時を入力してください（例: 10:30 / 2026/11/20 10:30 / 30分後）。'
    )

  tz = now.tzinfo or timezone.utc

  match = _RELATIVE_PATTERN.match(raw)
  if match:
    seconds = int(match.group('num')) * _UNIT_SECONDS[match.group('unit')]
    return now + timedelta(seconds=seconds)

  for pattern in _ABSOLUTE_PATTERNS:
    match = pattern.match(raw)
    if not match:
      continue
    groups = match.groupdict()
    hour = int(groups.get('H') or 0)
    minute = int(groups.get('M') or 0)
    if hour > 23 or minute > 59:
      raise ReminderError('時刻は 0:00 〜 23:59 の範囲で指定してください。')

    # 「今日」「明日」「明後日」+ 時刻
    if groups.get('day'):
      offset = _DAY_OFFSETS[groups['day']]
      base = now + timedelta(days=offset)
      return _build_datetime(
          base.year, base.month, base.day, hour, minute, tz
      )

    # 年つきの日付
    if groups.get('y'):
      return _build_datetime(
          int(groups['y']), int(groups['m']), int(groups['d']),
          hour, minute, tz,
      )

    # 年なしの日付（過ぎていれば来年として扱う）
    if groups.get('m'):
      month, day = int(groups['m']), int(groups['d'])
      candidate = _build_datetime(now.year, month, day, hour, minute, tz)
      if candidate < now - timedelta(days=180):
        candidate = _build_datetime(
            now.year + 1, month, day, hour, minute, tz
        )
      return candidate

    # 時刻のみ（すでに過ぎていれば明日）
    target = _build_datetime(now.year, now.month, now.day, hour, minute, tz)
    if target <= now:
      target += timedelta(days=1)
    return target

  raise ReminderError(
      '日時を解釈できませんでした。`10:30` `明日 9:00` `2026/11/20 10:30` '
      '`30分後` `2時間後` のように入力してください。'
  )


def format_datetime(moment: datetime, now: datetime | None = None) -> str:
  """Discordのタイムスタンプ表記と、読みやすい日時を返す。"""
  if moment.tzinfo is None:
    moment = moment.replace(tzinfo=reminder_store.tz)
  unix = int(moment.timestamp())
  now = now or reminder_store.now()
  if moment.date() == now.date():
    label = '今日'
  elif moment.date() == (now + timedelta(days=1)).date():
    label = '明日'
  else:
    label = moment.strftime('%Y/%m/%d')
  return f'{label} {moment.strftime("%H:%M")} (<t:{unix}:R>)'


def format_duration(delta: timedelta) -> str:
  """残り時間を「1時間 30分」のような読みやすい形式にする。"""
  total = int(delta.total_seconds())
  if total <= 0:
    return 'まもなく'
  # 1分未満は秒、1時間未満は分、それ以上は日・時間・分の上位2つで表す
  if total < 60:
    return f'{total}秒'
  units = (('日', 86400), ('時間', 3600), ('分', 60)) if total >= 3600 else (('分', 60),)
  parts = []
  for label, unit in units:
    value, total = divmod(total, unit)
    if value:
      parts.append(f'{value}{label}')
    if len(parts) == 2:
      break
  return ' '.join(parts)


# ---------------------------------------------------------------
# 5. 電卓の計算処理（安全に式を評価する）
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
# 6. 電卓のボタンUI
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


NO_MENTIONS = discord.AllowedMentions.none()
_MARKDOWN_SPECIAL = r'\`*_~|>[]()#'
MAX_MEMO_LENGTH = 1000
MAX_REMINDER_LENGTH = 1000
AUTOCOMPLETE_LIMIT = 25
MAX_REMINDER_ATTEMPTS = 5


def escape_markdown(text: str) -> str:
  """メモの内容がMarkdownとして解釈されないようにエスケープする。"""
  return ''.join('\\' + ch if ch in _MARKDOWN_SPECIAL else ch for ch in text)


def shorten(text: str, limit: int = 200) -> str:
  text = text.replace('\r', ' ').replace('\n', ' ⏎ ')
  if len(text) > limit:
    text = text[:limit] + '…'
  return text


# ---------------------------------------------------------------
# 7. ユーザーインストール対応のBotクラス
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
    # リマインダーの通知ループを開始する
    reminder_loop.start()

  async def on_ready(self):
    print(f"ログインしました: {client.user}")


client = MyBot()

# ---------------------------------------------------------------
# 8. スラッシュコマンドの定義
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


@memo_group.command(name='edit', description='指定した番号のメモを書き換えます')
@app_commands.describe(
    番号='編集するメモの番号（上から何番目か）。/memo list で確認できます',
    内容='新しいメモの内容',
)
async def memo_edit(
    interaction: discord.Interaction,
    番号: app_commands.Range[int, 1],
    内容: str,
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
    before, after = memo_store.edit(interaction.user.id, 番号, content)
  except MemoError as e:
    await interaction.response.send_message(
        f'⚠️ {e}', ephemeral=True, allowed_mentions=NO_MENTIONS
    )
    return

  await interaction.response.send_message(
      f'✏️ {番号}番目のメモを変更しました\n'
      f'> 変更前: {escape_markdown(shorten(before))}\n'
      f'> 変更後: {escape_markdown(shorten(after))}',
      allowed_mentions=NO_MENTIONS,
  )


@memo_group.command(name='search', description='キーワードを含むメモを検索します')
@app_commands.describe(キーワード='検索する文字列（大文字・小文字は区別しません）')
async def memo_search(
    interaction: discord.Interaction, キーワード: app_commands.Range[str, 1, 100]
):
  keyword = キーワード.strip()
  memos = memo_store.get(interaction.user.id)
  if not memos:
    await interaction.response.send_message(
        '📝 メモはまだ登録されていません。`/memo add` で追加できます。',
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
    return

  lowered = keyword.casefold()
  hits = [
      (index, memo)
      for index, memo in enumerate(memos, start=1)
      if lowered in memo.casefold()
  ]
  if not hits:
    await interaction.response.send_message(
        f'🔍 「{escape_markdown(shorten(keyword, 100))}」を含むメモは見つかりませんでした。',
        ephemeral=True,
        allowed_mentions=NO_MENTIONS,
    )
    return

  lines = [f'**{index}.** {escape_markdown(shorten(memo))}' for index, memo in hits]
  header = f'🔍 「{escape_markdown(shorten(keyword, 100))}」の検索結果（{len(hits)}件）\n'

  # 2000文字制限に収まるように分割する
  chunks: list[str] = []
  current = header
  for line in lines:
    if len(current) + len(line) + 1 > 1900:
      chunks.append(current.rstrip('\n'))
      current = ''
    current += line + '\n'
  if current.strip():
    chunks.append(current.rstrip('\n'))

  await interaction.response.send_message(chunks[0], allowed_mentions=NO_MENTIONS)
  for chunk in chunks[1:]:
    await interaction.followup.send(chunk, allowed_mentions=NO_MENTIONS)


@memo_edit.autocomplete('番号')
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


reminder_group = app_commands.Group(
    name='reminder', description='リマインダーの登録・一覧・削除'
)


def build_reminder_lines(items: list[dict], now: datetime) -> list[str]:
  lines = []
  for item in items:
    due_at = datetime.fromisoformat(item['due'])
    lines.append(
        f'**ID {item["id"]}** {escape_markdown(shorten(item["text"]))}\n'
        f'-# {format_datetime(due_at, now)} / あと{format_duration(due_at - now)}'
    )
  return lines


@reminder_group.command(
    name='add', description='指定した日時にリマインダーを登録します（DMで通知）'
)
@app_commands.describe(
    日時='「10:30」「明日 9:00」「2026/11/20 10:30」「30分後」のように指定',
    内容='通知する内容',
)
async def reminder_add(
    interaction: discord.Interaction,
    日時: app_commands.Range[str, 1, 100],
    内容: app_commands.Range[str, 1, MAX_REMINDER_LENGTH],
):
  now = reminder_store.now()
  try:
    due = parse_due(日時, now)
    item = reminder_store.add(
        interaction.user.id,
        内容.strip(),
        due,
        channel_id=getattr(interaction.channel, 'id', None),
    )
  except ReminderError as e:
    await interaction.response.send_message(
        f'⚠️ {e}', ephemeral=True, allowed_mentions=NO_MENTIONS
    )
    return

  due_at = datetime.fromisoformat(item['due'])
  await interaction.response.send_message(
      f'⏰ リマインダーを登録しました（ID {item["id"]}）\n'
      f'> {escape_markdown(shorten(item["text"]))}\n'
      f'-# {format_datetime(due_at, now)}（あと{format_duration(due_at - now)}）',
      allowed_mentions=NO_MENTIONS,
  )


@reminder_group.command(name='list', description='登録済みのリマインダーを表示します')
@app_commands.describe(完了済み='完了・通知済みのリマインダーも表示する')
async def reminder_list(
    interaction: discord.Interaction, 完了済み: bool = False
):
  now = reminder_store.now()
  items = reminder_store.list_for_user(interaction.user.id, include_done=完了済み)
  if not items:
    message = (
        '⏰ リマインダーは登録されていません。'
        '`/reminder add 日時: 30分後 内容: ...` で登録できます。'
    )
    await interaction.response.send_message(
        message, ephemeral=True, allowed_mentions=NO_MENTIONS
    )
    return

  lines = build_reminder_lines(items, now)
  header = f'⏰ **リマインダー一覧**（{len(items)}件）\n'

  chunks: list[str] = []
  current = header
  for line in lines:
    if len(current) + len(line) + 1 > 1900:
      chunks.append(current.rstrip('\n'))
      current = ''
    current += line + '\n'
  if current.strip():
    chunks.append(current.rstrip('\n'))

  await interaction.response.send_message(chunks[0], allowed_mentions=NO_MENTIONS)
  for chunk in chunks[1:]:
    await interaction.followup.send(chunk, allowed_mentions=NO_MENTIONS)


@reminder_group.command(name='delete', description='指定したIDのリマインダーを削除します')
@app_commands.describe(番号='削除するリマインダーのID（/reminder list で確認できます）')
async def reminder_delete(
    interaction: discord.Interaction, 番号: app_commands.Range[int, 1]
):
  try:
    removed = reminder_store.remove(interaction.user.id, 番号)
  except ReminderError as e:
    await interaction.response.send_message(
        f'⚠️ {e}', ephemeral=True, allowed_mentions=NO_MENTIONS
    )
    return

  await interaction.response.send_message(
      f'🗑️ リマインダー（ID {番号}）を削除しました\n'
      f'> {escape_markdown(shorten(removed["text"]))}',
      allowed_mentions=NO_MENTIONS,
  )


@reminder_delete.autocomplete('番号')
async def reminder_delete_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
  """削除するIDを選びやすくするため、登録済みのリマインダーを候補に出す。"""
  current = (current or '').strip()
  choices: list[app_commands.Choice[int]] = []
  for item in reminder_store.list_for_user(interaction.user.id):
    label = f'ID {item["id"]}: {shorten(item["text"], 60)}'
    if current and current not in label and not str(item['id']).startswith(current):
      continue
    choices.append(app_commands.Choice(name=label[:100], value=item['id']))
    if len(choices) >= AUTOCOMPLETE_LIMIT:
      break
  return choices


client.tree.add_command(memo_group)
client.tree.add_command(reminder_group)


# ---------------------------------------------------------------
# 9. リマインダーの通知ループ
# ---------------------------------------------------------------
@tasks.loop(seconds=30)
async def reminder_loop():
  """期限が来たリマインダーを30秒ごとに確認して通知する。"""
  for user_id, item in reminder_store.due():
    due_at = datetime.fromisoformat(item['due'])
    message = (
        f'⏰ **リマインダー**\n'
        f'> {escape_markdown(shorten(item["text"], 1500))}\n'
        f'-# 期限: {format_datetime(due_at)}'
    )
    delivered = False

    # まずはDMで通知する
    try:
      user = client.get_user(user_id) or await client.fetch_user(user_id)
      await user.send(message, allowed_mentions=NO_MENTIONS)
      delivered = True
    except Exception as e:  # noqa: BLE001 - DMが閉じている場合など
      print(f'リマインダーのDM送信に失敗しました user={user_id}: {e}')

    # DMに失敗したら、登録時のチャンネルに送る
    if not delivered and item.get('channel_id'):
      try:
        channel = client.get_channel(
            item['channel_id']
        ) or await client.fetch_channel(item['channel_id'])
        await channel.send(message, allowed_mentions=NO_MENTIONS)
        delivered = True
      except Exception as e:  # noqa: BLE001
        print(f'リマインダーの送信に失敗しました channel={item["channel_id"]}: {e}')

    # 送信できたものは完了扱いにする
    if delivered:
      reminder_store.mark_done(user_id, item['id'])
      continue

    # 送信できないまま MAX_REMINDER_ATTEMPTS 回失敗したら諦める（無限リトライ防止）
    attempts = reminder_store.record_attempt(user_id, item['id'])
    if attempts >= MAX_REMINDER_ATTEMPTS:
      reminder_store.mark_done(user_id, item['id'])
      print(
          f'リマインダーの通知を諦めました user={user_id} id={item["id"]} '
          f'（{attempts}回失敗）'
      )


@reminder_loop.error
async def reminder_loop_error(error: BaseException):
  print(f'リマインダーループでエラーが発生しました: {error}')


@reminder_loop.before_loop
async def reminder_loop_before():
  await client.wait_until_ready()





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
      name='⏰ `/reminder add 日時: 内容:`',
      value=(
          '指定した日時にDMで通知します（DMが送れない場合は実行したチャンネルに通知）。\n'
          '日時の指定例: `10:30` `明日 9:00` `明後日 9時` `2026/11/20 10:30` '
          '`30分後` `2時間後` `3日後` `1週間後`\n'
          '例: `/reminder add 日時:30分後 内容:洗濯物を取り込む`'
      ),
      inline=False,
  )
  embed.add_field(
      name='⏰ `/reminder list [完了済み:]`',
      value=(
          '登録済みのリマインダーを期限が近い順に表示します。\n'
          '`完了済み: True` を付けると通知済みのものも表示します。'
      ),
      inline=False,
  )
  embed.add_field(
      name='⏰ `/reminder delete 番号:`',
      value=(
          '指定したIDのリマインダーを削除します。\n'
          'IDは `/reminder list` で確認できます。'
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
          '例: `/memo add 内容:牛乳を買う` / `/memo add 内容:重要 位置:1`'
      ),
      inline=False,
  )
  embed.add_field(
      name='✏️ `/memo edit 番号: 内容:`',
      value=(
          '指定した番号のメモを書き換えます。\n'
          '番号は `/memo list` で確認できます。'
      ),
      inline=False,
  )
  embed.add_field(
      name='🔍 `/memo search キーワード:`',
      value='キーワードを含むメモを検索します（大文字・小文字は区別しません）。',
      inline=False,
  )
  embed.add_field(
      name='🗑️ `/memo delete 番号:`',
      value=(
          '指定した番号（上から何番目か）のメモを削除します。\n'
          '例: `/memo delete 番号:2`'
      ),
      inline=False,
  )
  embed.add_field(
      name='❓ `/help`',
      value='このコマンド一覧を表示します。',
      inline=False,
  )
  embed.set_footer(
      text=f'メモ・リマインダーはユーザーごとに保存されます（時刻は{reminder_store.timezone_name}）'
  )
  await interaction.response.send_message(
      embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS
  )


# 10. Webサーバーを起動してからBotを動かす
if __name__ == '__main__':
  keep_alive()
  client.run(os.environ.get('DISCORD_BOT_TOKEN'))
