"""
code_fixer 通用工具函数

覆盖: 路径安全校验、编码检测、信号处理、Shell 安全执行、脱敏
"""

import os
import re
import sys
import json
import hashlib
import platform
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List
from datetime import datetime, timezone

from config import (
    PATH_WHITELIST, ALLOWED_ERROR_TYPES, SENSITIVE_PATTERNS,
    IS_WINDOWS, PROJECT_ROOT,
)


# ═══════════════════════════════════════════════════════════════
# 路径安全
# ═══════════════════════════════════════════════════════════════

def validate_path(file_path: str) -> Path:
    """
    校验并规范化文件路径。
    - 仅允许白名单字符
    - 解析符号链接并确认在项目目录内
    - 返回解析后的绝对 Path，校验失败抛 ValueError
    """
    # 1. 白名单字符校验
    for ch in file_path:
        if ch not in PATH_WHITELIST:
            raise ValueError(f"路径包含不允许的字符: {repr(ch)}")

    path = Path(file_path).expanduser()

    # 2. 转为绝对路径
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    path = path.resolve()

    # 3. 解析符号链接
    try:
        real_path = path.resolve()
    except (OSError, RuntimeError):
        real_path = path

    # 4. 边界检查: 确认在项目目录内
    try:
        real_path.relative_to(PROJECT_ROOT)
    except ValueError:
        raise ValueError(
            f"文件路径超出项目目录范围:\n"
            f"  解析路径: {real_path}\n"
            f"  项目目录: {PROJECT_ROOT}"
        )

    return real_path


def is_symlink_outside_project(file_path: Path) -> bool:
    """检查符号链接是否指向项目外。返回 True 表示有风险。"""
    try:
        if file_path.is_symlink():
            real = file_path.resolve()
            try:
                real.relative_to(PROJECT_ROOT)
                return False
            except ValueError:
                return True
    except OSError:
        pass
    return False


# ═══════════════════════════════════════════════════════════════
# 编码检测
# ═══════════════════════════════════════════════════════════════

def detect_encoding(file_path: Path) -> str:
    """使用 chardet 检测文件编码，回退 UTF-8。"""
    try:
        import chardet
        with open(file_path, "rb") as f:
            raw = f.read(100_000)
        result = chardet.detect(raw)
        if result["confidence"] > 0.7:
            return result["encoding"]
    except ImportError:
        pass
    except Exception:
        pass

    # 先试 UTF-8，失败用系统默认
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            f.read(1024)
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"  # 不会抛异常的回退


def read_file_safe(file_path: Path) -> str:
    """安全读取文件，自动检测编码。"""
    encoding = detect_encoding(file_path)
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        return f.read()


def write_file_safe(file_path: Path, content: str, encoding: str = "utf-8") -> None:
    """安全写入文件。自动修复权限问题。"""
    try:
        with open(file_path, "w", encoding=encoding, newline="") as f:
            f.write(content)
    except PermissionError:
        # 尝试修复权限
        try:
            os.chmod(file_path, 0o644)
            with open(file_path, "w", encoding=encoding, newline="") as f:
                f.write(content)
        except Exception:
            raise


# ═══════════════════════════════════════════════════════════════
# 敏感信息脱敏
# ═══════════════════════════════════════════════════════════════

def redact_sensitive(code: str) -> Tuple[str, List[str]]:
    """
    检测并脱敏代码中的敏感信息。
    返回: (脱敏后代码, 发现的敏感类型列表)
    """
    found_types = []
    redacted = code

    for pattern, label in SENSITIVE_PATTERNS:
        matches = re.findall(pattern, redacted, re.IGNORECASE)
        if matches:
            found_types.append(label)
            redacted = re.sub(pattern, f'\\1="[REDACTED_{label.upper().replace(" ", "_")}]"', redacted)

    return redacted, found_types


# ═══════════════════════════════════════════════════════════════
# 错误日志脱敏
# ═══════════════════════════════════════════════════════════════

def sanitize_error_log(error_log: str, code_file_path: str) -> dict:
    """
    本地脱敏错误日志，仅提取结构化摘要。
    返回 Claude 友好的最小化信息。
    """
    # 提取错误类型
    error_type = "UnknownError"
    for et in ALLOWED_ERROR_TYPES:
        if et in error_log:
            error_type = et
            break

    # 提取文件名(仅保留 basename)
    basename = os.path.basename(code_file_path)

    # 提取行号
    line_match = re.search(r'line\s+(\d+)', error_log)
    line_number = int(line_match.group(1)) if line_match else None

    # 提取错误消息(截断至 200 字符)
    lines = error_log.strip().split("\n")
    last_line = lines[-1].strip() if lines else ""
    if len(last_line) > 200:
        last_line = last_line[:197] + "..."

    # 错误类型白名单校验
    if error_type == "UnknownError" and not any(et in error_log for et in ALLOWED_ERROR_TYPES):
        error_type = "UnknownError"

    return {
        "error_type": error_type,
        "file_basename": basename,
        "line_number": line_number,
        "error_message": last_line[:200],  # 硬截断 200 字符
    }


# ═══════════════════════════════════════════════════════════════
# Shell 安全执行
# ═══════════════════════════════════════════════════════════════

def run_shell(cmd: list, timeout: int = 60, cwd: Optional[Path] = None,
              silent_timeout: Optional[int] = None) -> dict:
    """
    安全执行 Shell 命令（参数化传参，非字符串拼接）。

    Args:
        cmd: 命令和参数列表
        timeout: 总超时秒数
        cwd: 工作目录
        silent_timeout: 静默超时（无输出秒数），None 则不启用

    Returns:
        {"stdout": str, "stderr": str, "exit_code": int, "timed_out": bool}
    """
    result = {
        "stdout": "",
        "stderr": "",
        "exit_code": -1,
        "timed_out": False,
    }

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            text=True,
            shell=False,  # 关键安全点: 不用 shell=True
        )

        try:
            if silent_timeout:
                # 静默超时模式: 监控无输出时间
                import select
                import time as time_mod
                last_output = time_mod.time()

                while proc.poll() is None:
                    if time_mod.time() - last_output > silent_timeout:
                        proc.kill()
                        result["timed_out"] = True
                        result["stderr"] = f"静默超时: {silent_timeout}s 无输出"
                        break

                    # 非阻塞读取
                    try:
                        if sys.platform == "win32":
                            time_mod.sleep(0.1)
                            # Windows 不支持 select on pipes, 用轮询
                            if proc.stdout:
                                line = proc.stdout.readline()
                                if line:
                                    result["stdout"] += line
                                    last_output = time_mod.time()
                        else:
                            readable, _, _ = select.select(
                                [proc.stdout, proc.stderr], [], [], 0.5
                            )
                            for stream in readable:
                                line = stream.readline()
                                if line:
                                    if stream is proc.stdout:
                                        result["stdout"] += line
                                    else:
                                        result["stderr"] += line
                                    last_output = time_mod.time()
                    except Exception:
                        pass
            else:
                # 普通超时模式
                stdout, stderr = proc.communicate(timeout=timeout)
                result["stdout"] = stdout or ""
                result["stderr"] = stderr or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            result["stdout"] = stdout or ""
            result["stderr"] = (stderr or "") + "\n[超时已终止]"
            result["timed_out"] = True

        result["exit_code"] = proc.returncode

    except FileNotFoundError:
        result["stderr"] = f"命令未找到: {cmd[0]}"
        result["exit_code"] = 127
    except Exception as e:
        result["stderr"] = str(e)
        result["exit_code"] = -1

    return result


# ═══════════════════════════════════════════════════════════════
# 信号处理
# ═══════════════════════════════════════════════════════════════

_cleanup_handlers = []


def register_cleanup(handler):
    """注册退出时的清理函数。"""
    _cleanup_handlers.append(handler)


def run_cleanup():
    """执行所有注册的清理函数。"""
    for handler in _cleanup_handlers:
        try:
            handler()
        except Exception:
            pass


def setup_signal_handlers():
    """设置信号处理 (trap EXIT INT TERM)。"""
    import signal

    def _handler(signum, frame):
        run_cleanup()
        sys.exit(1)

    for sig in [signal.SIGINT, signal.SIGTERM]:
        try:
            signal.signal(sig, _handler)
        except (ValueError, AttributeError):
            pass  # Windows 不支持部分信号

    import atexit
    atexit.register(run_cleanup)


# ═══════════════════════════════════════════════════════════════
# 其他工具
# ═══════════════════════════════════════════════════════════════

def format_duration_ms(start: datetime, end: datetime) -> int:
    """计算毫秒耗时。"""
    return int((end - start).total_seconds() * 1000)


def hash_content(content: str) -> str:
    """生成内容 SHA256 摘要。"""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def timestamp_str() -> str:
    """生成当前时间戳字符串。"""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def is_git_repo() -> bool:
    """检查当前目录是否为 Git 仓库。"""
    result = run_shell(["git", "rev-parse", "--git-dir"], timeout=5, cwd=PROJECT_ROOT)
    return result["exit_code"] == 0


def has_uncommitted_changes() -> bool:
    """检查是否有未提交的修改。"""
    result = run_shell(["git", "status", "--porcelain"], timeout=5, cwd=PROJECT_ROOT)
    return bool(result["stdout"].strip())


def init_git_repo():
    """初始化 Git 仓库并做初始提交。"""
    if not is_git_repo():
        run_shell(["git", "init"], timeout=10, cwd=PROJECT_ROOT)
        run_shell(["git", "add", "-A"], timeout=30, cwd=PROJECT_ROOT)
        run_shell(["git", "commit", "-m", "code_fixer: initial backup before automated fixes"],
                  timeout=10, cwd=PROJECT_ROOT)
        return True
    return False


def git_snapshot() -> Optional[str]:
    """保存 Git 快照 (stash + commit)。返回当前 HEAD SHA。"""
    result = run_shell(["git", "rev-parse", "HEAD"], timeout=5, cwd=PROJECT_ROOT)
    return result["stdout"].strip()[:8] if result["exit_code"] == 0 else None


def git_stash_push() -> bool:
    """暂存未提交修改。"""
    if has_uncommitted_changes():
        result = run_shell(["git", "stash", "push", "--include-untracked",
                            "-m", "code_fixer: auto stash before fix"],
                           timeout=10, cwd=PROJECT_ROOT)
        return result["exit_code"] == 0
    return True


def git_stash_pop() -> bool:
    """恢复暂存。"""
    result = run_shell(["git", "stash", "pop"], timeout=10, cwd=PROJECT_ROOT)
    return result["exit_code"] == 0


def git_diff_non_test_files(test_file_patterns: List[str]) -> List[str]:
    """检查是否有非测试/非白名单文件的改动。"""
    result = run_shell(["git", "diff", "--name-only"], timeout=10, cwd=PROJECT_ROOT)
    changed = result["stdout"].strip().split("\n") if result["stdout"].strip() else []
    # 白名单: 只有 _fixed.py / _test.py / .code_fixer/ .test.md 是允许的
    allowed = []
    for f in changed:
        f = f.strip()
        if not f:
            continue
        if f.endswith("_fixed.py") or f.endswith("_test.py") or ".code_fixer" in f or f.endswith(".test.md"):
            allowed.append(f)
        else:
            allowed.append(f)  # 现在包括所有改动，返回列表供调用方判断
    return [f for f in changed if f.strip()]


def git_restore_all():
    """恢复所有未提交的改动。"""
    run_shell(["git", "checkout", "--", "."], timeout=10, cwd=PROJECT_ROOT)


def detect_language(file_path: Path) -> Optional[str]:
    """根据文件扩展名检测编程语言。"""
    from config import LANGUAGE_DOCKER_IMAGES
    suffix = file_path.suffix.lower()
    for lang, cfg in LANGUAGE_DOCKER_IMAGES.items():
        if suffix in cfg["extensions"]:
            return lang
    return None


def scan_project_code_files() -> List[Path]:
    """
    扫描项目中的代码文件，跳过 .gitignore / node_modules / venv 等。
    返回支持的代码文件路径列表。
    """
    from config import LANGUAGE_DOCKER_IMAGES

    skip_dirs = {
        ".git", "node_modules", "venv", ".venv", "__pycache__",
        ".idea", ".vscode", "vendor", ".next", "dist", "build",
        "target", ".cache", ".code_fixer",
    }

    supported_exts = set()
    for cfg in LANGUAGE_DOCKER_IMAGES.values():
        supported_exts.update(cfg["extensions"])

    code_files = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        # 跳过不需要的目录
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

        for f in files:
            _, ext = os.path.splitext(f)
            if ext.lower() in supported_exts:
                code_files.append(Path(root) / f)

    return sorted(code_files)


def parse_dependency_declarations(test_code: str) -> List[str]:
    """
    解析测试代码中的依赖声明注释。
    格式: # @requires: pytest, numpy>=1.21
    """
    deps = []
    for line in test_code.split("\n"):
        match = re.match(r'#\s*@requires:\s*(.+)', line.strip())
        if match:
            deps.extend([d.strip() for d in match.group(1).split(",") if d.strip()])
    return deps
