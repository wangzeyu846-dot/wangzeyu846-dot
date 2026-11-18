#!/usr/bin/env python3
"""从公开仓库信息更新主页 Featured Projects；仅使用 Python 标准库。"""
from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

START = "<!-- featured-projects:start -->"
END = "<!-- featured-projects:end -->"
OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"


def _validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or not re.fullmatch(OWNER_PATTERN, owner):
        raise ValueError("GitHub 用户名格式不正确。")


def _validate_repository(repository: dict) -> None:
    if not isinstance(repository, dict) or not isinstance(repository.get("owner"), dict):
        raise ValueError("仓库数据结构不正确。")
    _validate_owner(repository["owner"].get("login"))
    name = repository.get("name")
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
            or name in (".", "..")):
        raise ValueError("仓库名称格式不正确。")
    if any(type(repository.get(flag)) is not bool for flag in ("private", "fork", "archived", "disabled")):
        raise ValueError("仓库可见性或状态信息不完整。")
    try:
        datetime.strptime(repository["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    except (KeyError, TypeError, ValueError):
        raise ValueError("仓库创建时间格式不正确。") from None
    for field in ("description", "language"):
        if repository.get(field) is not None and not isinstance(repository[field], str):
            raise ValueError("仓库简介或语言信息格式不正确。")
    topics = repository.get("topics", [])
    if not isinstance(topics, list) or any(not isinstance(topic, str) for topic in topics):
        raise ValueError("仓库标签格式不正确。")


def fetch_repositories(owner: str, token: str | None = None) -> list:
    _validate_owner(owner)
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "profile-featured-projects"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    repositories = []
    for page in range(1, 101):
        url = (f"https://api.github.com/users/{owner}/repos"
               f"?type=owner&sort=created&direction=desc&per_page=100&page={page}")
        try:
            with urlopen(Request(url, headers=headers), timeout=20) as response:
                batch = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub API 请求失败（HTTP {error.code}），保留现有主页。") from None
        except (URLError, OSError, TimeoutError):
            raise RuntimeError("GitHub API 网络请求失败，保留现有主页。") from None
        except (ValueError, UnicodeError):
            raise ValueError("GitHub API 返回的 JSON 无效，保留现有主页。") from None
        if not isinstance(batch, list):
            raise ValueError("GitHub API 未返回仓库列表，保留现有主页。")
        for repository in batch:
            _validate_repository(repository)
        repositories.extend(batch)
        if len(batch) < 100:
            return repositories
    raise RuntimeError("仓库分页超过安全上限，保留现有主页。")


def select_repositories(repositories: list, owner: str, limit: int = 6) -> list:
    _validate_owner(owner)
    if not isinstance(repositories, list) or type(limit) is not int or limit < 1:
        raise ValueError("仓库列表或展示数量不正确。")
    selected = []
    for repository in repositories:
        _validate_repository(repository)
        if (repository["owner"]["login"].lower() != owner.lower()
                or repository["name"].lower() == owner.lower()
                or any(repository[flag] for flag in ("private", "fork", "archived", "disabled"))):
            continue
        selected.append(repository)
    selected.sort(key=lambda item: (item["created_at"], item["name"].lower()), reverse=True)
    if not selected:
        raise ValueError("未找到可展示的公开项目，保留现有列表。")
    return selected[:limit]


def _cell(text: str) -> str:
    text = html.escape(" ".join(text.split()), quote=False)
    return re.sub(r"([\\|`\[\]*_~!])", r"\\\1", text)


def render_table(repositories: list) -> str:
    if not repositories:
        raise ValueError("不能用空列表替换现有项目。")
    lines = ["| 项目 | 简介 | 关键词 |", "| --- | --- | --- |"]
    for repository in repositories:
        _validate_repository(repository)
        name, owner = repository["name"], repository["owner"]["login"]
        description = repository.get("description") or "暂未填写项目简介"
        topics = repository.get("topics", [])[:3]
        keywords = " · ".join(topics) if topics else (repository.get("language") or "—")
        lines.append(f"| [{_cell(name)}](https://github.com/{owner}/{name}) | "
                     f"{_cell(description)} | {_cell(keywords)} |")
    return "\n".join(lines)


def update_readme(content: str, table: str) -> str:
    if content.count(START) != 1 or content.count(END) != 1:
        raise ValueError("README 自动维护标记缺失或重复，保留原文件。")
    start, end = content.index(START) + len(START), content.index(END)
    if start > end:
        raise ValueError("README 自动维护标记顺序不正确，保留原文件。")
    newline = "\r\n" if "\r\n" in content else "\n"
    return content[:start] + newline + table.replace("\n", newline) + newline + content[end:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--readme", default="README.md")
    args = parser.parse_args(argv)
    temporary = None
    try:
        path = Path(args.readme)
        with path.open(encoding="utf-8", newline="") as source:
            content = source.read()
        repositories = select_repositories(fetch_repositories(args.owner, os.environ.get("GITHUB_TOKEN")), args.owner)
        updated = update_readme(content, render_table(repositories))
        if updated == content:
            print("Featured Projects 已是最新，无需提交。")
            return 0
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                         prefix=".featured-", delete=False) as target:
            temporary = Path(target.name)
            target.write(updated)
        temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
        print(f"Featured Projects 已更新，共 {len(repositories)} 个项目。")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"同步失败：{error}", file=sys.stderr)
        return 1
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
