"""Botのロジック（電卓・メモ）のテスト。

実行方法: python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# テスト用の保存先を指定してから読み込む（本番の memos.json を汚さない）
_TEMP_DIR = tempfile.mkdtemp()
os.environ['MEMO_FILE'] = str(Path(_TEMP_DIR) / 'memos.json')
os.environ['REMINDER_FILE'] = str(Path(_TEMP_DIR) / 'reminders.json')
os.environ['TODO_FILE'] = str(Path(_TEMP_DIR) / 'todos.json')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402


class SafeEvalTest(unittest.TestCase):

  def test_basic_operations(self):
    cases = {
        '1+2*3': '7',
        '(1+2)*3': '9',
        '10/4': '2.5',
        '-5+3': '-2',
        '2**10': '1024',
        '7%3': '1',
        '5//2': '2',
        '0.1+0.2': '0.3',
    }
    for expression, expected in cases.items():
      with self.subTest(expression=expression):
        self.assertEqual(main.format_number(main.safe_eval(expression)), expected)

  def test_full_width_input(self):
    self.assertEqual(main.format_number(main.safe_eval('２×３')), '6')
    self.assertEqual(main.format_number(main.safe_eval('１０÷４')), '2.5')
    self.assertEqual(main.format_number(main.safe_eval('１，０００＋５００')), '1500')

  def test_rejects_unsafe_input(self):
    for expression in ["__import__('os')", 'import os', 'abs(-1)', 'x+1', '1+2;3', '']:
      with self.subTest(expression=expression):
        with self.assertRaises(main.CalcError):
          main.safe_eval(expression)

  def test_rejects_division_by_zero(self):
    with self.assertRaises(main.CalcError):
      main.safe_eval('1/0')
    with self.assertRaises(main.CalcError):
      main.safe_eval('1%0')

  def test_rejects_huge_results(self):
    with self.assertRaises(main.CalcError):
      main.safe_eval('2**99999')
    with self.assertRaises(main.CalcError):
      main.safe_eval('1e308*10')
    with self.assertRaises(main.CalcError):
      main.safe_eval('9' * 400)

  def test_rejects_too_long_expression(self):
    with self.assertRaises(main.CalcError):
      main.safe_eval('1+' * 200 + '1')


class MemoStoreTest(unittest.TestCase):

  def setUp(self):
    self.store = main.MemoStore(Path(tempfile.mkdtemp()) / 'memos.json')

  def test_add_appends_to_bottom_by_default(self):
    self.store.add(1, 'a')
    self.assertEqual(self.store.add(1, 'b'), 2)
    self.assertEqual(self.store.get(1), ['a', 'b'])

  def test_add_at_position(self):
    for text in ('a', 'b', 'c'):
      self.store.add(1, text)
    self.assertEqual(self.store.add(1, 'top', 1), 1)
    self.assertEqual(self.store.get(1), ['top', 'a', 'b', 'c'])

  def test_add_at_bottom_position(self):
    self.store.add(1, 'a')
    self.assertEqual(self.store.add(1, 'b', 2), 2)
    self.assertEqual(self.store.get(1), ['a', 'b'])

  def test_add_out_of_range(self):
    self.store.add(1, 'a')
    with self.assertRaises(main.MemoError):
      self.store.add(1, 'b', 5)

  def test_add_rejects_blank(self):
    with self.assertRaises(main.MemoError):
      self.store.add(1, '   ')

  def test_delete(self):
    for text in ('a', 'b', 'c'):
      self.store.add(1, text)
    self.assertEqual(self.store.delete(1, 2), 'b')
    self.assertEqual(self.store.get(1), ['a', 'c'])

  def test_delete_out_of_range(self):
    self.store.add(1, 'a')
    with self.assertRaises(main.MemoError):
      self.store.delete(1, 3)

  def test_delete_without_memos(self):
    with self.assertRaises(main.MemoError):
      self.store.delete(1, 1)

  def test_users_are_isolated(self):
    self.store.add(1, 'mine')
    self.assertEqual(self.store.get(2), [])

  def test_persistence(self):
    self.store.add(1, 'a')
    self.store.add(1, 'b')
    reloaded = main.MemoStore(self.store.path)
    self.assertEqual(reloaded.get(1), ['a', 'b'])

  def test_handles_broken_file(self):
    path = Path(tempfile.mkdtemp()) / 'memos.json'
    path.write_text('{ this is not json', encoding='utf-8')
    self.assertEqual(main.MemoStore(path).get(1), [])


class FormattingTest(unittest.TestCase):

  def test_escape_markdown(self):
    escaped = main.escape_markdown('**bold** `code`')
    self.assertIn('\\*', escaped)
    self.assertIn('\\`', escaped)

  def test_shorten(self):
    self.assertEqual(main.shorten('a\nb'), 'a ⏎ b')
    self.assertTrue(main.shorten('x' * 300).endswith('…'))

  def test_memo_lines(self):
    self.assertEqual(
        main.build_memo_lines(['a', 'b']), ['**1.** a', '**2.** b']
    )


class ParseDueTest(unittest.TestCase):
  """日時表現の解釈テスト（2026/11/15 14:30 を基準にする）。"""

  NOW = datetime(2026, 11, 15, 14, 30, tzinfo=main.JST)

  def parse(self, text):
    return main.parse_due(text, self.NOW)

  def test_relative(self):
    self.assertEqual(self.parse('30分後'), self.NOW + timedelta(minutes=30))
    self.assertEqual(self.parse('2時間後'), self.NOW + timedelta(hours=2))
    self.assertEqual(self.parse('3日後'), self.NOW + timedelta(days=3))
    self.assertEqual(self.parse('1週間後'), self.NOW + timedelta(weeks=1))
    self.assertEqual(self.parse('45分'), self.NOW + timedelta(minutes=45))

  def test_day_keywords(self):
    self.assertEqual(
        self.parse('明日 9:00'),
        datetime(2026, 11, 16, 9, 0, tzinfo=main.JST),
    )
    self.assertEqual(
        self.parse('今日 20:00'),
        datetime(2026, 11, 15, 20, 0, tzinfo=main.JST),
    )
    self.assertEqual(
        self.parse('明後日 9時'),
        datetime(2026, 11, 17, 9, 0, tzinfo=main.JST),
    )

  def test_absolute(self):
    expected = datetime(2026, 11, 20, 10, 30, tzinfo=main.JST)
    for text in ('2026/11/20 10:30', '2026-11-20 10:30', '11/20 10:30', '11月20日 10時30分'):
      with self.subTest(text=text):
        self.assertEqual(self.parse(text), expected)

  def test_time_only_rolls_to_tomorrow(self):
    self.assertEqual(
        self.parse('10:30'), datetime(2026, 11, 16, 10, 30, tzinfo=main.JST)
    )
    self.assertEqual(
        self.parse('20:00'), datetime(2026, 11, 15, 20, 0, tzinfo=main.JST)
    )

  def test_invalid(self):
    for text in ('', '   ', 'abc', '25:00', '13:70', '2026/2/30 10:00'):
      with self.subTest(text=text):
        with self.assertRaises(main.ReminderError):
          self.parse(text)

  def test_full_width(self):
    self.assertEqual(
        self.parse('２０２６／１１／２０ １０：３０'),
        datetime(2026, 11, 20, 10, 30, tzinfo=main.JST),
    )


class ReminderStoreTest(unittest.TestCase):

  def setUp(self):
    self.store = main.ReminderStore(Path(tempfile.mkdtemp()) / 'reminders.json')
    self.now = self.store.now()

  def test_add_and_get(self):
    item = self.store.add(1, 'test', self.now + timedelta(hours=1), channel_id=123)
    self.assertEqual(item['id'], 1)
    self.assertEqual(item['channel_id'], 123)
    self.assertEqual(self.store.get(1, 1)['text'], 'test')

  def test_add_rejects_past(self):
    with self.assertRaises(main.ReminderError):
      self.store.add(1, 'past', self.now - timedelta(days=1))

  def test_add_allows_almost_now(self):
    item = self.store.add(1, 'now', self.now - timedelta(seconds=5))
    self.assertGreater(
        datetime.fromisoformat(item['due']), self.now - timedelta(seconds=60)
    )

  def test_add_rejects_too_far(self):
    with self.assertRaises(main.ReminderError):
      self.store.add(1, 'far', self.now + timedelta(days=400))

  def test_add_rejects_blank(self):
    with self.assertRaises(main.ReminderError):
      self.store.add(1, '  ', self.now + timedelta(hours=1))

  def test_ids_increment(self):
    first = self.store.add(1, 'a', self.now + timedelta(hours=1))
    second = self.store.add(1, 'b', self.now + timedelta(hours=2))
    self.assertEqual((first['id'], second['id']), (1, 2))
    self.store.remove(1, 1)
    third = self.store.add(1, 'c', self.now + timedelta(hours=3))
    self.assertEqual(third['id'], 3)

  def test_list_sorted_and_hides_done(self):
    late = self.store.add(1, 'late', self.now + timedelta(hours=5))
    early = self.store.add(1, 'early', self.now + timedelta(hours=1))
    self.assertEqual(
        [item['id'] for item in self.store.list_for_user(1)],
        [early['id'], late['id']],
    )
    self.store.mark_done(1, early['id'])
    self.assertEqual(
        [item['id'] for item in self.store.list_for_user(1)], [late['id']]
    )
    self.assertEqual(len(self.store.list_for_user(1, include_done=True)), 2)

  def test_remove_and_errors(self):
    item = self.store.add(1, 'a', self.now + timedelta(hours=1))
    self.assertEqual(self.store.remove(1, item['id'])['text'], 'a')
    with self.assertRaises(main.ReminderError):
      self.store.remove(1, item['id'])
    with self.assertRaises(main.ReminderError):
      self.store.mark_done(1, 999)

  def test_users_are_isolated(self):
    self.store.add(1, 'mine', self.now + timedelta(hours=1))
    self.assertEqual(self.store.list_for_user(2), [])

  def test_due(self):
    item = self.store.add(1, 'soon', self.now + timedelta(minutes=5))
    self.assertEqual(self.store.due(), [])
    ready = self.store.due(self.now + timedelta(minutes=10))
    self.assertEqual([i['id'] for _, i in ready], [item['id']])
    self.store.mark_done(1, item['id'])
    self.assertEqual(self.store.due(self.now + timedelta(minutes=10)), [])

  def test_persistence(self):
    self.store.add(1, 'a', self.now + timedelta(hours=1), channel_id=42)
    reloaded = main.ReminderStore(self.store.path)
    item = reloaded.list_for_user(1)[0]
    self.assertEqual((item['text'], item['channel_id']), ('a', 42))

  def test_handles_broken_file(self):
    path = Path(tempfile.mkdtemp()) / 'reminders.json'
    path.write_text('not json', encoding='utf-8')
    self.assertEqual(main.ReminderStore(path).list_for_user(1), [])

  def test_attempts(self):
    item = self.store.add(1, 'a', self.now + timedelta(hours=1))
    self.assertEqual(self.store.record_attempt(1, item['id']), 1)
    self.assertEqual(self.store.record_attempt(1, item['id']), 2)


class MemoEditTest(unittest.TestCase):

  def setUp(self):
    self.store = main.MemoStore(Path(tempfile.mkdtemp()) / 'memos.json')
    for text in ('a', 'b'):
      self.store.add(1, text)

  def test_edit(self):
    self.assertEqual(self.store.edit(1, 2, 'B'), ('b', 'B'))
    self.assertEqual(self.store.get(1), ['a', 'B'])

  def test_edit_errors(self):
    with self.assertRaises(main.MemoError):
      self.store.edit(1, 1, '   ')
    with self.assertRaises(main.MemoError):
      self.store.edit(1, 9, 'x')
    with self.assertRaises(main.MemoError):
      self.store.edit(2, 1, 'x')


class FormatDurationTest(unittest.TestCase):

  def test_format_duration(self):
    self.assertEqual(main.format_duration(timedelta(seconds=30)), '30秒')
    self.assertEqual(main.format_duration(timedelta(minutes=5)), '5分')
    self.assertEqual(main.format_duration(timedelta(minutes=90)), '1時間 30分')
    self.assertEqual(main.format_duration(timedelta(days=2, hours=3)), '2日 3時間')
    self.assertEqual(main.format_duration(timedelta(seconds=0)), 'まもなく')


class TodoStoreTest(unittest.TestCase):

  def setUp(self):
    self.store = main.TodoStore(Path(tempfile.mkdtemp()) / 'todos.json', main.JST)
    self.now = self.store.now()

  def test_add_and_get(self):
    item = self.store.add(1, 'task', '高', self.now + timedelta(hours=1))
    self.assertEqual((item['id'], item['priority'], item['done']), (1, '高', False))
    self.assertEqual(self.store.get(1, 1)['text'], 'task')

  def test_add_without_due(self):
    item = self.store.add(1, 'no due')
    self.assertIsNone(item['due'])
    self.assertEqual(item['priority'], '中')

  def test_add_errors(self):
    with self.assertRaises(main.TodoError):
      self.store.add(1, '   ')
    with self.assertRaises(main.TodoError):
      self.store.add(1, 'x', '最優先')

  def test_ids_increment(self):
    first = self.store.add(1, 'a')
    second = self.store.add(1, 'b')
    self.assertEqual((first['id'], second['id']), (1, 2))
    self.store.remove(1, 1)
    self.assertEqual(self.store.add(1, 'c')['id'], 3)

  def test_sorting_open_first_then_priority_then_due(self):
    low = self.store.add(1, 'low', '低', self.now + timedelta(hours=1))
    high = self.store.add(1, 'high', '高')
    mid = self.store.add(1, 'mid', '中', self.now + timedelta(hours=2))
    done = self.store.add(1, 'done', '高')
    self.store.set_done(1, done['id'])
    order = [item['id'] for item in self.store.list_for_user(1)]
    self.assertEqual(order, [high['id'], mid['id'], low['id']])
    all_items = [item['id'] for item in self.store.list_for_user(1, include_done=True)]
    self.assertEqual(all_items[-1], done['id'])

  def test_set_done_and_undo(self):
    item = self.store.add(1, 'task')
    self.assertTrue(self.store.set_done(1, item['id'])['done'])
    self.assertFalse(self.store.set_done(1, item['id'], done=False)['done'])
    with self.assertRaises(main.TodoError):
      self.store.set_done(1, 999)

  def test_remove(self):
    item = self.store.add(1, 'task')
    self.assertEqual(self.store.remove(1, item['id'])['text'], 'task')
    with self.assertRaises(main.TodoError):
      self.store.remove(1, item['id'])

  def test_clear_done(self):
    open_item = self.store.add(1, 'open')
    done_item = self.store.add(1, 'done')
    self.store.set_done(1, done_item['id'])
    self.assertEqual(self.store.clear_done(1), 1)
    self.assertEqual([i['id'] for i in self.store.list_for_user(1, include_done=True)], [open_item['id']])
    self.assertEqual(self.store.clear_done(1), 0)

  def test_users_are_isolated(self):
    self.store.add(1, 'mine')
    self.assertEqual(self.store.list_for_user(2), [])

  def test_stats(self):
    self.store.add(1, 'no due')
    self.store.add(1, 'today', '中', self.now)
    self.store.add(1, 'overdue', '高', self.now - timedelta(days=1))
    done = self.store.add(1, 'done')
    self.store.set_done(1, done['id'])
    stats = self.store.stats(1)
    self.assertEqual(stats, {'total': 4, 'open': 3, 'done': 1, 'today': 1, 'overdue': 1})

  def test_due_reminders_only_once(self):
    item = self.store.add(1, 'soon', '中', self.now + timedelta(minutes=5))
    self.assertEqual(self.store.due_reminders(), [])
    ready = self.store.due_reminders(self.now + timedelta(minutes=10))
    self.assertEqual([i['id'] for _, i in ready], [item['id']])
    self.store.mark_notified(1, item['id'])
    self.assertEqual(self.store.due_reminders(self.now + timedelta(minutes=10)), [])

  def test_due_reminders_skips_done(self):
    item = self.store.add(1, 'done', '中', self.now - timedelta(minutes=5))
    self.store.set_done(1, item['id'])
    self.assertEqual(self.store.due_reminders(), [])

  def test_persistence(self):
    self.store.add(1, 'a', '高', self.now + timedelta(hours=2))
    reloaded = main.TodoStore(self.store.path, main.JST)
    item = reloaded.list_for_user(1)[0]
    self.assertEqual((item['text'], item['priority']), ('a', '高'))
    self.assertIsNotNone(item['due'])

  def test_handles_broken_file(self):
    path = Path(tempfile.mkdtemp()) / 'todos.json'
    path.write_text('{ broken', encoding='utf-8')
    self.assertEqual(main.TodoStore(path, main.JST).list_for_user(1), [])


class TodoFormatTest(unittest.TestCase):

  NOW = datetime(2026, 11, 15, 14, 30, tzinfo=main.JST)

  def test_format_due_short(self):
    self.assertEqual(main.format_due_short(None, self.NOW), '期限なし')
    today = (self.NOW + timedelta(hours=3)).isoformat()
    self.assertTrue(main.format_due_short(today, self.NOW).startswith('今日'))
    tomorrow = (self.NOW + timedelta(days=1)).replace(hour=0, minute=0).isoformat()
    self.assertTrue(main.format_due_short(tomorrow, self.NOW).startswith('明日'))
    far = datetime(2026, 12, 24, 0, 0, tzinfo=main.JST).isoformat()
    self.assertIn('2026/12/24', main.format_due_short(far, self.NOW))
    past = (self.NOW - timedelta(days=2)).isoformat()
    self.assertIn('期限切れ', main.format_due_short(past, self.NOW))

  def test_is_due_expired(self):
    self.assertFalse(main.is_due_expired(None, self.NOW))
    self.assertTrue(
        main.is_due_expired((self.NOW - timedelta(minutes=1)).isoformat(), self.NOW)
    )
    self.assertFalse(
        main.is_due_expired((self.NOW + timedelta(minutes=1)).isoformat(), self.NOW)
    )

  def test_split_chunks(self):
    lines = [f'line{i}' for i in range(400)]
    chunks = main.split_chunks('header\n', lines)
    self.assertGreater(len(chunks), 1)
    for chunk in chunks:
      self.assertLessEqual(len(chunk), 1900)
    self.assertEqual(''.join(chunks).count('line'), 400)
    self.assertEqual(main.split_chunks('header\n', []), ['header'])


class NotificationLoopTest(unittest.IsolatedAsyncioTestCase):
  """通知ループの配送テスト（Discordへの通信はモックする）。"""

  async def asyncSetUp(self):
    self.sent = []
    self.dm_fail_for = set()

    sent = self.sent
    dm_fail_for = self.dm_fail_for

    class FakeUser:

      def __init__(self, uid):
        self.id = uid

      async def send(self, message, **kwargs):
        if self.id in dm_fail_for:
          raise RuntimeError('DM closed')
        sent.append(('dm', self.id, message))

    class FakeChannel:

      def __init__(self, cid):
        self.id = cid

      async def send(self, message, **kwargs):
        sent.append(('channel', self.id, message))

    main.client.get_user = lambda uid: FakeUser(uid)
    main.client.get_channel = lambda cid: FakeChannel(cid) if cid == 777 else None

    async def fail_fetch(cid):
      raise RuntimeError('unknown channel')

    main.client.fetch_channel = fail_fetch

    # テストごとに空のストアを使う
    temp = Path(tempfile.mkdtemp())
    self.reminders = main.ReminderStore(temp / 'reminders.json')
    self.todos = main.TodoStore(temp / 'todos.json', main.JST)
    self._orig_reminders = main.reminder_store
    self._orig_todos = main.todo_store
    main.reminder_store = self.reminders
    main.todo_store = self.todos

  async def asyncTearDown(self):
    main.reminder_store = self._orig_reminders
    main.todo_store = self._orig_todos

  def _add_due_reminder(self, uid, text, channel_id=None):
    now = self.reminders.now()
    item = self.reminders.add(uid, text, now + timedelta(minutes=1), channel_id)
    for bucket in self.reminders._data[str(uid)]:
      if bucket['id'] == item['id']:
        bucket['due'] = (now - timedelta(seconds=5)).isoformat()
    self.reminders._save()
    return item

  def _add_due_todo(self, uid, text):
    now = self.todos.now()
    item = self.todos.add(uid, text, '中', now + timedelta(minutes=1))
    for bucket in self.todos._data[str(uid)]:
      if bucket['id'] == item['id']:
        bucket['due'] = (now - timedelta(seconds=5)).isoformat()
    self.todos._save()
    return item

  async def test_reminder_dm_is_preferred(self):
    item = self._add_due_reminder(1, 'dm reminder', channel_id=777)
    await main.process_due_notifications()
    self.assertEqual(self.sent[0][0], 'dm')
    self.assertTrue(self.reminders.get(1, item['id'])['done'])

  async def test_reminder_falls_back_to_channel(self):
    self.dm_fail_for.add(2)
    item = self._add_due_reminder(2, 'channel reminder', channel_id=777)
    await main.process_due_notifications()
    self.assertEqual([(kind, cid) for kind, cid, _ in self.sent], [('channel', 777)])
    self.assertTrue(self.reminders.get(2, item['id'])['done'])

  async def test_reminder_gives_up_after_max_attempts(self):
    self.dm_fail_for.add(3)
    item = self._add_due_reminder(3, 'nowhere', channel_id=111)
    for _ in range(main.MAX_REMINDER_ATTEMPTS):
      self.assertEqual(len(self.reminders.due()), 1)
      await main.process_due_notifications()
    self.assertEqual(self.reminders.due(), [])
    self.assertEqual(self.reminders.get(3, item['id'])['attempts'], main.MAX_REMINDER_ATTEMPTS)

  async def test_todo_notified_only_once(self):
    item = self._add_due_todo(1, 'todo')
    await main.process_due_notifications()
    self.assertEqual(len(self.sent), 1)
    self.assertTrue(self.sent[0][2].startswith('📋'))
    self.assertEqual(self.todos.due_reminders(), [])
    await main.process_due_notifications()
    self.assertEqual(len(self.sent), 1)
    self.assertTrue(self.todos.get(1, item['id'])['notified'])

  async def test_todo_marks_notified_even_if_dm_fails(self):
    self.dm_fail_for.add(2)
    self._add_due_todo(2, 'closed dm')
    await main.process_due_notifications()
    self.assertEqual(self.sent, [])
    self.assertEqual(self.todos.due_reminders(), [])


if __name__ == '__main__':
  unittest.main()
