"""
code_fixer Docker 沙盒模块

功能:
- Docker 可用性检测 + 挂载测试
- 沙盒内代码执行
- 资源限制 (内存/CPU/进程数)
- 跨平台兼容 (Windows/macOS/Linux)
"""

import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List

from config import (
    DOCKER_IMAGE_DEFAULT, DOCKER_TIMEOUT_SEC, DOCKER_MEMORY_LIMIT,
    DOCKER_CPU_LIMIT, DOCKER_PIDS_LIMIT, DOCKER_NETWORK_MODE,
    LANGUAGE_DOCKER_IMAGES, IS_WINDOWS, IS_MACOS, PROJECT_ROOT,
)
from utils import run_shell, format_duration_ms


def check_docker_installed() -> bool:
    """检查 Docker 是否已安装并可运行。"""
    result = run_shell(["docker", "--version"], timeout=5)
    return result["exit_code"] == 0


def check_docker_running() -> bool:
    """检查 Docker daemon 是否在运行。"""
    result = run_shell(["docker", "info"], timeout=10)
    return result["exit_code"] == 0


def check_docker_mount(work_dir: Optional[Path] = None) -> dict:
    """
    测试 Docker 文件挂载是否正常。
    返回: {"ok": bool, "error": str, "platform_hint": str}
    """
    test_dir = work_dir or PROJECT_ROOT
    test_cmd = [
        "docker", "run", "--rm",
        "-v", f"{test_dir}:/test_mount",
        "python:3.11-slim",
        "ls", "/test_mount",
    ]

    result = run_shell(test_cmd, timeout=30)

    if result["exit_code"] == 0:
        return {"ok": True, "error": "", "platform_hint": ""}

    # 给出平台特异性指引
    hints = {
        "win32": "请打开 Docker Desktop → Settings → Resources → File Sharing → 添加此盘符",
        "darwin": "请打开 Docker Desktop → Preferences → Resources → File Sharing → 添加此目录",
        "linux": "请确认当前用户在 docker 组中: sudo usermod -aG docker $USER",
    }
    import sys
    platform_hint = hints.get(sys.platform, "")

    return {
        "ok": False,
        "error": result["stderr"],
        "platform_hint": platform_hint,
    }


def run_in_docker(code: str, test_code: str, language: str,
                  dependencies: Optional[List[str]] = None,
                  timeout_sec: int = DOCKER_TIMEOUT_SEC) -> dict:
    """
    在 Docker 沙盒中执行代码和测试。

    Args:
        code: 被测试的代码文件内容
        test_code: 测试代码内容
        language: 编程语言
        dependencies: 需要安装的依赖包列表
        timeout_sec: 超时秒数

    Returns:
        {"passed": bool, "logs": str, "exit_code": int, "execution_time_ms": int}
    """
    lang_cfg = LANGUAGE_DOCKER_IMAGES.get(language)
    if not lang_cfg:
        return {
            "passed": False,
            "logs": f"不支持的语言: {language}",
            "exit_code": -1,
            "execution_time_ms": 0,
        }

    image = lang_cfg["image"]
    start_time = datetime.now(timezone.utc)

    # 创建临时目录存放代码和测试
    with tempfile.TemporaryDirectory(prefix="code_fixer_docker_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # 确定文件扩展名
        exts = lang_cfg["extensions"]
        ext = exts[0] if exts else ".py"

        code_file = tmp_path / f"code{ext}"
        test_file = tmp_path / f"test{ext}"

        # 写入代码和测试
        code_file.write_text(code, encoding="utf-8")
        test_file.write_text(test_code, encoding="utf-8")

        # 构建 Docker 命令
        # 基础命令: 在容器内先安装依赖(如有)，然后执行测试
        inner_script = _build_inner_script(language, lang_cfg, dependencies, ext)

        docker_cmd = [
            "docker", "run", "--rm",
            "--network", DOCKER_NETWORK_MODE,
            "--memory", DOCKER_MEMORY_LIMIT,
            "--cpus", str(DOCKER_CPU_LIMIT),
            "--pids-limit", str(DOCKER_PIDS_LIMIT),
            "-v", f"{tmp_dir}:/workspace",
            "-w", "/workspace",
            image,
            "sh", "-c", inner_script,
        ]

        result = run_shell(docker_cmd, timeout=timeout_sec)
        end_time = datetime.now(timezone.utc)

        passed = result["exit_code"] == 0 and not result["timed_out"]

        logs = result["stdout"]
        if result["stderr"]:
            logs += "\n" + result["stderr"]
        if result["timed_out"]:
            logs += "\n[Docker 执行超时]"

        return {
            "passed": passed,
            "logs": logs.strip(),
            "exit_code": result["exit_code"],
            "execution_time_ms": format_duration_ms(start_time, end_time),
        }


def _build_inner_script(language: str, lang_cfg: dict,
                        dependencies: Optional[List[str]], ext: str) -> str:
    """构建 Docker 容器内执行的 shell 脚本。"""
    parts = []

    # 安装依赖
    if dependencies:
        install_cmd = lang_cfg.get("install_cmd", "")
        if install_cmd:
            # 替换 requirements.txt 为实际依赖
            if "{file}" not in install_cmd and "requirements.txt" in install_cmd:
                # 逐个安装依赖
                dep_str = " ".join(dependencies)
                parts.append(f"pip install {dep_str} 2>&1 || true")
            else:
                parts.append(f"{install_cmd} 2>&1 || true")

    # 语法检查
    check_cmd = lang_cfg["check_cmd"].format(file=f"code{ext}")
    parts.append(f"echo '=== SYNTAX CHECK ==='")
    parts.append(f"{check_cmd} 2>&1 || true")

    # 执行测试
    test_cmd = lang_cfg["test_cmd"]
    if "{test_file}" in test_cmd:
        test_cmd = test_cmd.format(test_file=f"test{ext}")
    elif "{file}" in test_cmd:
        test_cmd = test_cmd.format(file=f"test{ext}")

    parts.append(f"echo '=== TEST EXECUTION ==='")
    parts.append(f"{test_cmd} 2>&1")

    return " && ".join(parts)


def pull_docker_image(image: str = DOCKER_IMAGE_DEFAULT) -> bool:
    """拉取 Docker 镜像。"""
    result = run_shell(["docker", "pull", image], timeout=120)
    return result["exit_code"] == 0


def cleanup_docker_resources():
    """清理 code_fixer 相关的 Docker 资源。"""
    # 停止并删除 code_fixer 容器
    run_shell(["docker", "ps", "-a", "--filter", "name=code_fixer", "--format", "{{.ID}}"], timeout=10)
    run_shell(["docker", "container", "prune", "-f"], timeout=30)

    # 清理悬空镜像
    run_shell(["docker", "image", "prune", "-f"], timeout=30)
