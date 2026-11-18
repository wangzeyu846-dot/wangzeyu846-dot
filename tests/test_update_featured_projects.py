import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/update_featured_projects.py"
spec = importlib.util.spec_from_file_location("featured", SCRIPT)
featured = importlib.util.module_from_spec(spec)
spec.loader.exec_module(featured)
START = "<!-- featured-projects:start -->"
END = "<!-- featured-projects:end -->"


def repo(name="project", created="2026-01-01T00:00:00Z", **changes):
    value = dict(name=name, owner={"login": "demo"}, created_at=created,
                 private=False, fork=False, archived=False, disabled=False,
                 description="一个项目", topics=["ai", "product"], language="Python")
    value.update(changes)
    return value


class SelectionTests(unittest.TestCase):
    def test_filters_and_orders_six_newest(self):
        items = [repo(f"p{i}", f"2026-01-{i:02}T00:00:00Z") for i in range(1, 9)]
        items += [repo("DEMO"), repo("other", owner={"login": "another"})]
        items += [repo(flag, **{flag: True}) for flag in ("private", "fork", "archived", "disabled")]
        self.assertEqual([r["name"] for r in featured.select_repositories(items, "demo")],
                         ["p8", "p7", "p6", "p5", "p4", "p3"])

    def test_malformed_repository_is_rejected(self):
        for changes in ({"private": "false"}, {"name": "../evil"}, {"owner": None},
                        {"created_at": "yesterday"}, {"topics": "ai"}, {"description": {}},
                        {"language": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                featured.select_repositories([repo(**changes)], "demo")

    def test_empty_eligible_list_is_not_published(self):
        with self.assertRaises(ValueError):
            featured.select_repositories([repo(private=True)], "demo")

    def test_owner_input_is_validated(self):
        for owner in ("../demo", "demo?x=1", "", "-invalid"):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                featured.select_repositories([repo()], owner)


class RenderingTests(unittest.TestCase):
    def test_escape_description_and_limit_topics(self):
        result = featured.render_table([repo(description='A | B\r\n<img> [x](https://evil) *bold* `code` &',
                                            topics=["one", "two", "three", "four"])])
        self.assertIn("A \\| B &lt;img&gt; \\[x\\](https://evil)", result)
        self.assertIn("\\*bold\\* \\`code\\` &amp;", result)
        self.assertIn("one · two · three", result)
        self.assertNotIn("four", result)
        self.assertEqual(len(result.splitlines()), 3)

    def test_fallbacks_and_url_from_owner_name(self):
        result = featured.render_table([repo(description=None, topics=[], language="Python",
                                            html_url="https://evil.example")])
        self.assertIn("暂未填写项目简介", result)
        self.assertIn("Python", result)
        self.assertIn("https://github.com/demo/project", result)
        self.assertNotIn("evil.example", result)
        self.assertIn("—", featured.render_table([repo(topics=[], language=None)]))

    def test_outside_text_is_unchanged_and_update_is_idempotent(self):
        original = f"个人介绍\r\n{START}\r\n旧内容\r\n{END}\r\n其余内容\r\n"
        changed = featured.update_readme(original, "新表格")
        self.assertEqual(changed.split(START)[0], original.split(START)[0])
        self.assertEqual(changed.split(END)[1], original.split(END)[1])
        self.assertIn("新表格", changed)
        self.assertEqual(featured.update_readme(changed, "新表格"), changed)

    def test_missing_duplicate_or_reversed_markers_fail(self):
        for content in ("no markers", START, END, END + START,
                        START + START + END, START + END + END):
            with self.subTest(content=content), self.assertRaises(ValueError):
                featured.update_readme(content, "new")


class FetchTests(unittest.TestCase):
    def test_paginated_request_and_timeout(self):
        first = [repo(f"p{i}") for i in range(100)]
        with patch.object(featured, "urlopen", side_effect=[io.BytesIO(json.dumps(first).encode()),
                          io.BytesIO(json.dumps([repo("last")]).encode())]) as opened:
            result = featured.fetch_repositories("demo", "private-test-token")
        self.assertEqual(len(result), 101)
        self.assertIn("page=2", opened.call_args_list[1].args[0].full_url)
        self.assertIn("type=owner", opened.call_args_list[0].args[0].full_url)
        self.assertGreater(opened.call_args_list[0].kwargs["timeout"], 0)

    def test_bad_json_and_unexpected_response_fail(self):
        for data in (b"invalid", b'{"message":"error"}', b"[null]"):
            with self.subTest(data=data), patch.object(featured, "urlopen", return_value=io.BytesIO(data)):
                with self.assertRaises(ValueError):
                    featured.fetch_repositories("demo")

    def test_http_error_does_not_expose_token(self):
        error = HTTPError("https://api.github.com", 403, "private-test-token", {}, None)
        with patch.object(featured, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                featured.fetch_repositories("demo", "private-test-token")
        self.assertNotIn("private-test-token", str(caught.exception))

    def test_error_on_later_page_does_not_return_partial_data(self):
        pages = [io.BytesIO(json.dumps([repo(f"p{i}") for i in range(100)]).encode()), TimeoutError()]
        with patch.object(featured, "urlopen", side_effect=pages), self.assertRaises(RuntimeError):
            featured.fetch_repositories("demo")


class CommandTests(unittest.TestCase):
    def run_main(self, path):
        return featured.main(["--owner", "demo", "--readme", str(path)])

    def test_api_failure_leaves_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            original = f"介绍\r\n{START}\r\n旧表\r\n{END}\r\n末尾".encode()
            path.write_bytes(original)
            with patch.object(featured, "fetch_repositories", side_effect=RuntimeError("请求失败")), redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_main(path), 1)
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_markers_leave_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            path.write_bytes(b"no markers\r\n")
            with patch.object(featured, "fetch_repositories", return_value=[repo()]), redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_main(path), 1)
            self.assertEqual(path.read_bytes(), b"no markers\r\n")

    def test_success_preserves_outer_bytes_and_second_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            path.write_bytes(f"介绍\r\n{START}\r\n旧表\r\n{END}\r\n末尾".encode())
            with patch.object(featured, "fetch_repositories", return_value=[repo()]):
                self.assertEqual(self.run_main(path), 0)
                content, modified = path.read_bytes(), path.stat().st_mtime_ns
                self.assertTrue(content.startswith(f"介绍\r\n{START}".encode()))
                self.assertTrue(content.endswith(f"{END}\r\n末尾".encode()))
                self.assertEqual(self.run_main(path), 0)
                self.assertEqual(path.stat().st_mtime_ns, modified)


if __name__ == "__main__":
    unittest.main()
