"""
code_fixer 代码安全扫描器

功能:
- 测试用例危险模式检测
- 代码语法检查
- PyPI 包名 typo-squatting 检测
"""

import re
import os
import json
from pathlib import Path
from typing import List, Tuple, Optional

from config import FORBIDDEN_TEST_PATTERNS, LANGUAGE_DOCKER_IMAGES
from utils import run_shell, detect_language


# ═══════════════════════════════════════════════════════════════
# 危险模式扫描
# ═══════════════════════════════════════════════════════════════

def scan_test_code(test_code: str) -> dict:
    """
    扫描测试用例中的危险模式。
    返回: {"safe": bool, "violations": [{"pattern": str, "line": str, "line_num": int}]}
    """
    violations = []
    lines = test_code.split("\n")

    for i, line in enumerate(lines, 1):
        for pattern in FORBIDDEN_TEST_PATTERNS:
            if re.search(pattern, line):
                violations.append({
                    "pattern": pattern,
                    "line": line.strip(),
                    "line_num": i,
                })
                break  # 一行只报告第一个违规

    return {
        "safe": len(violations) == 0,
        "violations": violations,
    }


# ═══════════════════════════════════════════════════════════════
# 语法检查
# ═══════════════════════════════════════════════════════════════

def check_syntax(code: str, file_path: Path) -> dict:
    """
    用目标语言的解释器做语法检查。
    返回: {"valid": bool, "error": str}
    """
    language = detect_language(file_path)
    if not language:
        return {"valid": True, "error": ""}

    lang_cfg = LANGUAGE_DOCKER_IMAGES.get(language)
    if not lang_cfg:
        return {"valid": True, "error": ""}

    # 写入临时文件
    import tempfile
    suffix = file_path.suffix
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name

    try:
        check_cmd_str = lang_cfg["check_cmd"].format(file=tmp_path)
        check_cmd = check_cmd_str.split()

        result = run_shell(check_cmd, timeout=30)
        return {
            "valid": result["exit_code"] == 0,
            "error": result["stderr"] if result["exit_code"] != 0 else "",
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════
# PyPI 安全检查
# ═══════════════════════════════════════════════════════════════

# 知名 PyPI 包白名单 (按下载量排序的前几百个，用于 typo-squatting 检测)
_KNOWN_PYPI_PACKAGES: Optional[set] = None


def _get_known_packages() -> set:
    """加载知名 PyPI 包列表。首次调用时从本地缓存加载。"""
    global _KNOWN_PYPI_PACKAGES
    if _KNOWN_PYPI_PACKAGES is not None:
        return _KNOWN_PYPI_PACKAGES

    # 内置知名包列表（top 200+）
    _KNOWN_PYPI_PACKAGES = {
        "numpy", "pandas", "requests", "flask", "django", "pytest", "pytest-cov",
        "black", "ruff", "mypy", "isort", "pre-commit", "tox", "coverage",
        "sqlalchemy", "alembic", "psycopg2", "psycopg2-binary", "redis",
        "celery", "pillow", "matplotlib", "seaborn", "scipy", "scikit-learn",
        "tensorflow", "torch", "transformers", "openai", "anthropic", "langchain",
        "fastapi", "uvicorn", "gunicorn", "pydantic", "httpx", "aiohttp",
        "beautifulsoup4", "lxml", "scrapy", "selenium", "playwright",
        "click", "typer", "rich", "tqdm", "loguru", "structlog",
        "pyyaml", "toml", "python-dotenv", "python-dateutil", "pytz",
        "jinja2", "markupsafe", "werkzeug", "itsdangerous",
        "boto3", "azure-storage-blob", "google-cloud-storage",
        "cryptography", "bcrypt", "passlib", "pyjwt", "oauthlib",
        "websockets", "socketio", "python-socketio",
        "networkx", "sympy", "nltk", "spacy", "textblob",
        "opencv-python", "moviepy", "pydub",
        "dash", "streamlit", "gradio", "plotly",
        "ipython", "jupyter", "notebook", "jupyterlab",
        "pytest-mock", "pytest-xdist", "pytest-asyncio", "pytest-timeout",
        "faker", "factory-boy", "hypothesis",
        "attrs", "dataclasses-json", "marshmallow",
        "asyncpg", "aiosqlite", "motor",
        "sphinx", "mkdocs", "mkdocs-material",
        "pyinstaller", "cx-freeze", "nuitka",
        "wheel", "setuptools", "pip", "pip-tools", "pipenv", "poetry",
        "virtualenv", "pipx", "twine",
        "orjson", "ujson", "msgpack", "protobuf",
        "diskcache", "python-diskcache", "cachetools",
        "tenacity", "retry", "backoff",
        "watchdog", "watchfiles", "inotify",
        "charset-normalizer", "chardet", "idna", "urllib3", "certifi",
        "arrow", "pendulum", "python-dotenv",
        "more-itertools", "toolz", "funcy",
        "inflection", "python-slugify", "unidecode",
        "colorama", "termcolor", "blessed",
        "tabulate", "prettytable", "texttable",
        "humanize", "humanfriendly",
        "deprecated", "wrapt", "decorator",
        "sortedcontainers", "blist", "bintrees",
        "python-levenshtein", "jellyfish", "rapidfuzz",
        "regex", "re2",
        "pygments", "pyparsing", "ply",
        "json5", "hjson", "tomli", "tomli-w",
        "python-magic", "filetype", "mimetypes",
        "pillow-heif", "python-resize-image",
        "django-rest-framework", "django-cors-headers", "django-filter",
        "fastapi-users", "fastapi-cache", "fastapi-pagination",
        "flask-sqlalchemy", "flask-migrate", "flask-login", "flask-cors",
        "flask-restful", "flask-jwt-extended",
    }
    return _KNOWN_PYPI_PACKAGES


def check_dependency_safety(dependencies: List[str]) -> dict:
    """
    检查依赖包安全性。
    - typo-squatting 检测
    - 已知问题源警告
    返回: {"safe": bool, "warnings": List[str], "suspicious": List[str]}
    """
    warnings = []
    suspicious = []

    known = _get_known_packages()

    for dep in dependencies:
        # 提取包名 (去掉版本约束)
        pkg_name = re.split(r'[=<>~!]', dep)[0].strip().lower()
        if not pkg_name:
            continue

        # Typo-squatting 检测: 与知名包名编辑距离
        if pkg_name not in known:
            similar = _find_similar_package(pkg_name, known)
            if similar:
                suspicious.append(
                    f"包 '{pkg_name}' 未在知名 PyPI 列表中。你是否想安装 '{similar}'？"
                )
            else:
                warnings.append(
                    f"包 '{pkg_name}' 不在知名 PyPI 列表中，请注意安全"
                )

    return {
        "safe": len(suspicious) == 0,
        "warnings": warnings,
        "suspicious": suspicious,
    }


def _find_similar_package(name: str, known: set, max_distance: int = 2) -> Optional[str]:
    """查找编辑距离在阈值内的知名包名。"""
    # 简单启发式: 检查常见的 typo-squatting 模式
    name_lower = name.lower()

    # 精确匹配
    if name_lower in known:
        return None

    # 检查常见的 typo 变体
    for known_name in known:
        known_l = known_name.lower()
        # - 连字符/下划线交换
        if name_lower.replace("-", "_") == known_l.replace("-", "_"):
            return known_name
        # - 单字符差异
        if _levenshtein_distance(name_lower, known_l) == 1:
            return known_name

    return None


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Levenshtein 编辑距离。"""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insert = prev_row[j + 1] + 1
            delete = curr_row[j] + 1
            sub = prev_row[j] + (c1 != c2)
            curr_row.append(min(insert, delete, sub))
        prev_row = curr_row

    return prev_row[-1]
