"""
code_fixer 配置管理

安全要求：
- API Key 从 Claude Code settings.json 环境变量读取，不硬编码
- 所有路径配置跨平台兼容
- 敏感配置不写入日志
"""

import os
import sys
import json
import platform
from pathlib import Path
from typing import Optional


# ── 平台检测 ──────────────────────────────────────────────
IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"

# ── 路径常量 ──────────────────────────────────────────────
PROJECT_ROOT = Path(os.getcwd())
SKILL_DIR = Path(__file__).resolve().parent.parent  # code_fixer/
SCRIPTS_DIR = SKILL_DIR / "scripts"
HISTORY_DIR = SKILL_DIR / "history"
TEMP_DIR = Path("/tmp/code_fixer") if not IS_WINDOWS else Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "code_fixer"
LOCK_FILE_NAME = ".code_fixer.lock"
CONFIG_DIR = PROJECT_ROOT / ".code_fixer"

# ── API 配置 (从环境变量读取，由 Claude Code settings.json 注入) ──
CLAUDE_API_KEY = os.environ.get("CODEFIXER_CLAUDE_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))
CLAUDE_API_BASE = os.environ.get("CODEFIXER_CLAUDE_API_BASE", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"))
CODEX_API_KEY = os.environ.get("CODEFIXER_CODEX_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
CODEX_API_BASE = os.environ.get("CODEFIXER_CODEX_API_BASE", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))

# ── Docker 配置 ────────────────────────────────────────────
DOCKER_IMAGE_DEFAULT = "python:3.11-slim"
DOCKER_TIMEOUT_SEC = 30
DOCKER_MEMORY_LIMIT = "256m"
DOCKER_CPU_LIMIT = 1.0
DOCKER_PIDS_LIMIT = 50
DOCKER_NETWORK_MODE = "none"  # 完全断网

# ── 语言 Docker 镜像映射（可扩展） ─────────────────────────
LANGUAGE_DOCKER_IMAGES = {
    "python": {
        "image": "python:3.11-slim",
        "check_cmd": "python -m py_compile {file}",
        "test_cmd": "python {test_file}",
        "extensions": [".py"],
        "dependency_files": ["requirements.txt", "pyproject.toml", "Pipfile"],
        "install_cmd": "pip install -r requirements.txt",
    },
    "typescript": {
        "image": "node:20-slim",
        "check_cmd": "npx tsc --noEmit {file}",
        "test_cmd": "npx ts-node {test_file}",
        "extensions": [".ts", ".tsx"],
        "dependency_files": ["package.json"],
        "install_cmd": "npm install",
    },
    "javascript": {
        "image": "node:20-slim",
        "check_cmd": "node --check {file}",
        "test_cmd": "node {test_file}",
        "extensions": [".js", ".mjs"],
        "dependency_files": ["package.json"],
        "install_cmd": "npm install",
    },
    "go": {
        "image": "golang:1.22-slim",
        "check_cmd": "go vet {file}",
        "test_cmd": "go test {test_file}",
        "extensions": [".go"],
        "dependency_files": ["go.mod"],
        "install_cmd": "go mod download",
    },
}

# ── 安全规则 ──────────────────────────────────────────────
# 禁止在测试用例中使用的危险模式
FORBIDDEN_TEST_PATTERNS = [
    r"os\.system\s*\(",
    r"subprocess\s*\.\s*\w+",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"__import__\s*\(",
    r"shutil\.rmtree\s*\(",
    r"os\.remove\s*\(",
    r"os\.unlink\s*\(",
    r"os\.rmdir\s*\(",
    r"importlib\.import_module\s*\(",
    r"compile\s*\(.*,\s*['\"]exec['\"]",
]

# 敏感信息检测正则 (高熵字符串模式)
SENSITIVE_PATTERNS = [
    (r'(?:api[_-]?key|apikey|API_KEY)\s*[:=]\s*["\']([^"\'\s]{20,})["\']', "API Key"),
    (r'(?:token|TOKEN)\s*[:=]\s*["\']([^"\'\s]{20,})["\']', "Token"),
    (r'(?:password|passwd|pwd)\s*[:=]\s*["\']([^"\'\s]+)["\']', "Password"),
    (r'(?:secret|SECRET)\s*[:=]\s*["\']([^"\'\s]{10,})["\']', "Secret"),
    (r'sk-[a-zA-Z0-9]{20,}', "OpenAI API Key"),
    (r'ghp_[a-zA-Z0-9]{20,}', "GitHub Token"),
    (r'(?:dsn|DATABASE_URL)\s*[:=]\s*["\']([^"\']+)["\']', "Database URL"),
    (r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', "IP Address"),
]

# 路径白名单字符: 字母、数字、下划线、连字符、点、斜杠、冒号(Windows盘符)
PATH_WHITELIST = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-. /:\\")

# 允许的错误类型白名单
ALLOWED_ERROR_TYPES = [
    "ZeroDivisionError", "SyntaxError", "TypeError", "ValueError", "KeyError",
    "IndexError", "AttributeError", "NameError", "FileNotFoundError", "IOError",
    "ImportError", "ModuleNotFoundError", "IndentationError", "TabError",
    "UnboundLocalError", "RuntimeError", "NotImplementedError", "RecursionError",
    "AssertionError", "StopIteration", "StopAsyncIteration", "GeneratorExit",
    "FloatingPointError", "OverflowError", "ReferenceError", "SystemError",
    "MemoryError", "BufferError", "ConnectionError", "TimeoutError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError",
    "OSError", "PermissionError", "FileExistsError", "IsADirectoryError",
    "NotADirectoryError", "InterruptedError", "ProcessLookupError",
    "BlockingIOError", "ChildProcessError", "BrokenPipeError",
    "ConnectionAbortedError", "ConnectionRefusedError", "ConnectionResetError",
]

# ── Token 与费用配置 ──────────────────────────────────────
# DeepSeek V4 Pro 定价 (示例，可根据实际 API 提供商调整)
PRICING = {
    "claude": {"input_per_1k": 0.014, "output_per_1k": 0.028},   # 人民币元
    "codex": {"input_per_1k": 0.014, "output_per_1k": 0.028},
}

# 费用限制
GLOBAL_COST_WARNING_RMB = 10.0     # 全局超 10 元警告
GLOBAL_COST_HARD_LIMIT_RMB = 20.0  # 全局硬上限
SINGLE_FILE_HISTORY_MAX = 5        # 保留最近 5 次会话

# ── Codex CLI 配置 ────────────────────────────────────────
CODEX_CLI_PACKAGE = "@openai/codex@0"
CODEX_CLI_SILENT_TIMEOUT_SEC = 60  # 代码生成阶段静默超时
CODEX_CLI_TEST_TIMEOUT_SEC = 120   # 测试执行阶段静默超时

# ── 重试与容错 ────────────────────────────────────────────
MAX_RETRY_ATTEMPTS = 3           # 单文件最大重试次数
API_TIMEOUT_SEC = 5              # API 调用超时
API_RETRY_COUNT = 2              # API 超时重试次数
API_RETRY_DELAY_SEC = 1          # API 重试间隔
RATE_LIMIT_WAIT_SEC = 5          # 429 限流等待基数
RATE_LIMIT_MAX_WAIT_SEC = 300    # 指数退避最大等待
LOCK_TIMEOUT_MINUTES = 30        # 锁文件过期时间
MAX_LINES_BEFORE_SLICING = 500   # 超过此行数做智能切片

# ── 确认点配置 ────────────────────────────────────────────
# 需要用户确认的阶段
CONFIRM_BEFORE_FIX = True        # Claude 分析后确认
CONFIRM_BEFORE_WRITE = True      # 最终写入前确认
CONFIRM_BEFORE_DEP_INSTALL = True # 依赖安装前确认


def validate_config() -> dict:
    """
    启动时验证配置完整性，返回检查结果。
    """
    result = {
        "claude_api_key": bool(CLAUDE_API_KEY),
        "codex_api_key": bool(CODEX_API_KEY),
        "claude_api_accessible": False,
        "codex_cli_installed": False,
        "docker_available": False,
        "docker_mount_works": False,
        "git_available": False,
        "errors": [],
        "warnings": [],
    }

    # 检查项目目录
    if not PROJECT_ROOT.exists():
        result["errors"].append(f"项目目录不存在: {PROJECT_ROOT}")

    return result
