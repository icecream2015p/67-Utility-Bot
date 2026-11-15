"""Botのロジック（電卓・メモ）のテスト。

実行方法: python -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# テスト用の保存先を指定してから読み込む（本番の memos.json を汚さない）
_TEMP_DIR = tempfile.mkdtemp()
os.environ['MEMO_FILE'] = str(Path(_TEMP_DIR) / 'memos.json')
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


if __name__ == '__main__':
  unittest.main()
