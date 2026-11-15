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


if __name__ == '__main__':
  unittest.main()
