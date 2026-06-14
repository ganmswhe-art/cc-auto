"""
code_fixer Tier 2 — 单文件修复流程

新流程 (Codex Desktop):
  阶段1 prepare: Claude 分析 → 生成任务文件 → 启动 Codex 桌面
  阶段2 verify:  用户确认 Codex 完成 → 读取修复结果 → 验证 → 写入

用法:
  python fix.py --file src/utils.py --analysis analysis.json --phase prepare
  python fix.py --file src/utils.py --analysis analysis.json --phase verify
"""

import os
import sys
import json
import shutil
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    CODEX_API_KEY, CODEX_API_BASE, CODEX_CLI_PACKAGE,
    MAX_RETRY_ATTEMPTS, PROJECT_ROOT, CONFIG_DIR, LANGUAGE_DOCKER_IMAGES,
)
from utils import (
    validate_path, read_file_safe, write_file_safe, detect_language,
    sanitize_error_log, redact_sensitive, run_shell, format_duration_ms,
    parse_dependency_declarations, git_stash_push, git_stash_pop,
    git_diff_non_test_files, git_restore_all, timestamp_str,
)
from code_scanner import scan_test_code, check_syntax, check_dependency_safety
from docker_sandbox import (
    check_docker_installed, check_docker_running, run_in_docker,
)
from cost_tracker import CostTracker
from session_manager import SessionManager


# ═══════════════════════════════════════════════════════════════
# 辅助: 格式化测试用例
# ═══════════════════════════════════════════════════════════════

def _format_test_cases(test_cases):
    """将测试用例列表格式化为 Markdown 列表。"""
    if test_cases:
        return chr(10).join("- " + tc for tc in test_cases)
    else:
        lines = [
            "- 修复原始错误",
            "- 测试边界条件",
            "- 回归测试原有功能",
        ]
        return chr(10).join(lines)


# ═══════════════════════════════════════════════════════════════
# 阶段 1: 准备
# ═══════════════════════════════════════════════════════════════

def prepare_codex_task(file_path, analysis, cost_tracker, session_mgr,
                       attempt=1, previous_error=None):
    """
    阶段 1: Claude 分析错误 → 生成 Codex Desktop 任务文件 → 启动 Codex 桌面。
    """
    # 验证路径
    try:
        resolved_path = validate_path(file_path)
    except ValueError as e:
        return {"status": "failed", "reason": str(e)}

    # 读取源文件
    try:
        original_code = read_file_safe(resolved_path)
    except Exception as e:
        return {"status": "failed", "reason": "读取文件失败: " + str(e)}

    language = detect_language(resolved_path)
    if not language:
        return {"status": "failed",
                "reason": "不支持的文件类型: " + str(resolved_path.suffix)}

    # 脱敏
    redacted_code, sensitive_info = redact_sensitive(original_code)

    # Git 保护
    git_stash_push()

    # -- Claude 分析 --
    if attempt == 1:
        claude_result = analysis
    else:
        prev_err = previous_error or {}
        claude_result = _call_claude_analyze(
            redacted_code, prev_err,
            analysis.get("fix_plan", ""), attempt,
        )
        cost_tracker.record_tier2(
            file_path, "claude",
            claude_result.get("input_tokens", 0),
            claude_result.get("output_tokens", 0),
        )

    fix_plan = claude_result.get("fix_plan", "")
    root_cause = claude_result.get("root_cause", "")
    test_cases = claude_result.get("test_cases", [])

    # -- 生成 Codex Desktop 任务文件 --
    task_content = _build_codex_desktop_task(
        original_code=original_code,
        fix_plan=fix_plan,
        root_cause=root_cause,
        language=language,
        file_name=resolved_path.name,
        test_cases=test_cases,
        attempt=attempt,
    )

    task_file = CONFIG_DIR / "codex_task.md"
    task_file.write_text(task_content, encoding="utf-8")

    code_file = CONFIG_DIR / "codex_original.py"
    code_file.write_text(original_code, encoding="utf-8")

    plan_text = "# 修复方案" + chr(10) + chr(10) + fix_plan
    plan_text += chr(10) + chr(10) + "# 测试用例" + chr(10) + chr(10)
    plan_text += chr(10).join(test_cases)
    plan_file = CONFIG_DIR / "codex_fix_plan.md"
    plan_file.write_text(plan_text, encoding="utf-8")

    # -- 启动 Codex Desktop --
    workspace = str(resolved_path.parent.resolve())
    launch_result = _launch_codex_desktop(workspace)

    # -- 保存会话状态 --
    session_mgr.init_session()
    session_mgr.save_session_state({
        "stage": "tier2_codex_waiting",
        "file_path": file_path,
        "attempt": attempt,
        "claude_result": claude_result,
        "cost_tracker": cost_tracker.to_dict(),
        "status": "waiting_for_codex",
    })

    result_template = {
        "status": "pending",
        "file_path": str(resolved_path),
        "attempt": attempt,
    }
    result_file = CONFIG_DIR / "codex_result.json"
    result_file.write_text(
        json.dumps(result_template, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    instruction = _build_user_instruction(task_file, resolved_path, workspace)

    return {
        "status": "waiting_for_codex",
        "task_file": str(task_file),
        "code_file": str(code_file),
        "plan_file": str(plan_file),
        "result_file": str(result_file),
        "workspace": workspace,
        "root_cause": root_cause,
        "fix_plan": fix_plan,
        "language": language,
        "attempt": attempt,
        "instruction": instruction,
    }


def _build_user_instruction(task_file, file_path, workspace):
    """生成给用户的操作指引。"""
    nl = chr(10)
    return (
        nl + "Codex Desktop 已启动!" + nl + nl +
        "请在 Codex Desktop 中执行以下操作:" + nl + nl +
        "1. 确认工作区: " + workspace + nl +
        "2. 打开任务文件: " + str(task_file) + nl +
        "3. 将任务内容粘贴给 Codex，让它修复代码" + nl +
        "4. 观察 Codex 修复过程" + nl +
        "5. Codex 完成后，复制它生成的修复代码" + nl +
        "6. 粘贴到以下文件: " + str(CONFIG_DIR / "codex_fixed.py") + nl +
        "7. 把 Codex 的测试结果粘贴到: " + str(CONFIG_DIR / "codex_test_result.md") + nl +
        '8. 回到这里对我说 "done" 或 "Codex 已完成"' + nl + nl +
        "等待你的确认..." + nl
    )


# ═══════════════════════════════════════════════════════════════
# 阶段 2: 验证
# ═══════════════════════════════════════════════════════════════

def verify_codex_result(file_path, cost_tracker, session_mgr):
    """
    阶段 2: 读取 Codex Desktop 产生的修复代码 → 安全扫描 → 验证 → 写入。
    """
    # 恢复会话状态
    incomplete = session_mgr.load_incomplete_sessions()
    current_session = None
    for s in incomplete:
        if s["state"].get("file_path") == file_path:
            current_session = s
            break

    attempt = 1
    claude_result = {}
    if current_session:
        attempt = current_session["state"].get("attempt", 1)
        claude_result = current_session["state"].get("claude_result", {})

    try:
        resolved_path = validate_path(file_path)
    except ValueError as e:
        return {"status": "failed", "reason": str(e)}

    try:
        original_code = read_file_safe(resolved_path)
    except Exception as e:
        return {"status": "failed", "reason": "读取文件失败: " + str(e)}

    language = detect_language(resolved_path)
    if not language:
        return {"status": "failed",
                "reason": "不支持的文件类型: " + str(resolved_path.suffix)}

    # 读取 Codex 产出
    fixed_code_file = CONFIG_DIR / "codex_fixed.py"
    if not fixed_code_file.exists():
        return {
            "status": "failed",
            "reason": (
                "未找到 Codex 修复结果: " + str(fixed_code_file) +
                "。请在 Codex Desktop 中完成修复并保存。"
            ),
        }

    try:
        fixed_code = fixed_code_file.read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "failed", "reason": "读取结果失败: " + str(e)}

    if not fixed_code.strip():
        return {"status": "failed", "reason": "Codex 修复结果为空"}

    test_result_file = CONFIG_DIR / "codex_test_result.md"
    codex_test_logs = ""
    if test_result_file.exists():
        codex_test_logs = test_result_file.read_text(encoding="utf-8")

    # 语法检查
    tmp_fixed = CONFIG_DIR / ("tmp_fixed_" + timestamp_str() + resolved_path.suffix)
    write_file_safe(tmp_fixed, fixed_code)
    syntax_check = check_syntax(fixed_code, resolved_path)
    if not syntax_check["valid"]:
        return {
            "status": "failed",
            "reason": "语法错误: " + syntax_check["error"],
            "fixed_code": fixed_code,
            "attempt": attempt,
        }

    # 测试代码提取与安全扫描
    test_code = _extract_test_code(codex_test_logs, language)
    if test_code:
        scan_result = scan_test_code(test_code)
        if not scan_result["safe"]:
            parts = []
            for v in scan_result["violations"]:
                parts.append(
                    "  行 " + str(v["line_num"]) + ": " + v["pattern"]
                )
            return {
                "status": "failed",
                "reason": "危险模式:" + chr(10) + chr(10).join(parts),
                "fixed_code": fixed_code,
                "attempt": attempt,
            }

    dependencies = parse_dependency_declarations(test_code)

    # Docker 验证
    docker_available = check_docker_installed() and check_docker_running()
    result = None

    if docker_available:
        full_test = test_code if test_code else (
            "# 基本测试" + chr(10) + "print('loaded')" + chr(10)
        )
        docker_result = run_in_docker(
            fixed_code, full_test, language, dependencies,
        )

        if docker_result["passed"]:
            fixed_path = _get_fixed_path(resolved_path)
            write_file_safe(fixed_path, fixed_code)
            run_shell(["git", "add", str(fixed_path)], timeout=5, cwd=PROJECT_ROOT)
            run_shell(
                ["git", "commit", "-m",
                 "code_fixer: backup before apply fix to " + file_path],
                timeout=10, cwd=PROJECT_ROOT,
            )
            result = {
                "status": "success",
                "original_code": original_code,
                "fixed_code": fixed_code,
                "fix_explanation": claude_result.get("fix_plan", ""),
                "test_result": docker_result,
                "codex_test_logs": codex_test_logs,
                "attempts": attempt,
                "total_cost_rmb": cost_tracker.file_cost(file_path),
                "fixed_file_path": str(fixed_path),
            }
        else:
            user_opts = {}
            if attempt < MAX_RETRY_ATTEMPTS:
                user_opts = {
                    "skip": "跳过此文件",
                    "retry": "重新分析 (第 " + str(attempt + 1) + "/" + str(MAX_RETRY_ATTEMPTS) + " 轮)",
                }
            else:
                user_opts = {
                    "skip": "跳过此文件",
                    "claude_fix": "让 Claude 直接修复",
                }
            result = {
                "status": "failed",
                "reason": "Docker 验证失败: " + docker_result.get("logs", "")[:500],
                "fixed_code": fixed_code,
                "test_result": docker_result,
                "attempt": attempt,
                "total_cost_rmb": cost_tracker.file_cost(file_path),
                "needs_user_decision": True,
                "user_options": user_opts,
            }
    else:
        # Docker 不可用，信任 Codex Desktop
        fixed_path = _get_fixed_path(resolved_path)
        write_file_safe(fixed_path, fixed_code)
        result = {
            "status": "success",
            "original_code": original_code,
            "fixed_code": fixed_code,
            "fix_explanation": claude_result.get("fix_plan", ""),
            "test_result": {
                "passed": True,
                "logs": codex_test_logs or "(Codex Desktop 测试 - Docker 未安装)",
            },
            "codex_test_logs": codex_test_logs,
            "attempts": attempt,
            "total_cost_rmb": cost_tracker.file_cost(file_path),
            "fixed_file_path": str(fixed_path),
            "warning": "Docker 未安装，仅依赖 Codex Desktop 测试结果",
        }

    git_stash_pop()
    if result:
        session_mgr.mark_session_completed()
        session_mgr.save_file_record(file_path, result)

    return result


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

def _build_codex_desktop_task(original_code, fix_plan, root_cause,
                               language, file_name, test_cases, attempt):
    """生成给 Codex Desktop 的任务 Markdown 文件。"""
    nl = chr(10)
    formatted_tests = _format_test_cases(test_cases)

    return (
        "# Codex 修复任务 #" + str(attempt) + nl + nl +
        "> 复制以下全部内容到 Codex Desktop 对话框中。" + nl + nl +
        "---" + nl + nl +
        "## 你的任务" + nl + nl +
        "你是代码修复专家。请根据以下信息：" + nl +
        "1. 修复代码中的所有错误" + nl +
        "2. 编写测试用例验证修复" + nl +
        "3. 运行测试确保所有测试通过" + nl +
        "4. 输出修复后的完整代码" + nl + nl +
        "---" + nl + nl +
        "## 原始代码 (`" + file_name + "`)" + nl + nl +
        "```" + language + nl +
        original_code + nl +
        "```" + nl + nl +
        "---" + nl + nl +
        "## 错误分析 (由 Claude 提供)" + nl + nl +
        "**根因**: " + root_cause + nl + nl +
        "**修复方案**:" + nl +
        fix_plan + nl + nl +
        "---" + nl + nl +
        "## 测试要求" + nl + nl +
        "请编写并运行测试用例，覆盖以下场景：" + nl +
        formatted_tests + nl + nl +
        "---" + nl + nl +
        "## 输出要求" + nl + nl +
        "完成后，请输出：" + nl + nl +
        "1. **修复后的完整代码** (保存到 `.code_fixer/codex_fixed.py`)" + nl +
        "2. **测试执行结果** (保存到 `.code_fixer/codex_test_result.md`)" + nl + nl +
        "格式:" + nl +
        "```" + nl +
        "===FIXED_CODE===" + nl +
        "<完整的修复后代码>" + nl +
        "===END_FIXED_CODE===" + nl + nl +
        "===TEST_RESULTS===" + nl +
        "<测试执行日志>" + nl +
        "===END_TEST_RESULTS===" + nl +
        "```" + nl
    )


def _launch_codex_desktop(workspace):
    """启动 Codex Desktop 应用。"""
    result = run_shell(["codex", "app", workspace], timeout=15)
    already = "already running" in result["stderr"].lower()
    return {
        "launched": result["exit_code"] == 0 or already,
        "exit_code": result["exit_code"],
        "stderr": result["stderr"],
    }


def _call_claude_analyze(code, error_info, prev_fix_plan, attempt):
    """调用 Claude API 重新分析错误。"""
    import anthropic

    client = anthropic.Anthropic(
        api_key=os.environ.get(
            "CODEFIXER_CLAUDE_API_KEY",
            os.environ.get("ANTHROPIC_API_KEY", ""),
        ),
    )

    nl = chr(10)
    system_prompt = (
        "你是代码调试助手。分析以下代码和错误，输出 JSON:" + nl +
        "{" + nl +
        '  "root_cause": "错误根因",' + nl +
        '  "fix_plan": "详细修复方案",' + nl +
        '  "test_cases": ["测试用例1", "测试用例2"],' + nl +
        '  "line_numbers": [需修改的行号]' + nl +
        "}"
    )

    user_prompt = (
        "## 当前代码" + nl +
        "```python" + nl + code + nl + "```" + nl + nl +
        "## 错误信息" + nl +
        "- 类型: " + error_info.get("error_type", "Unknown") + nl +
        "- 文件: " + error_info.get("file_basename", "") + nl +
        "- 行号: " + str(error_info.get("line_number", "N/A")) + nl +
        "- 消息: " + error_info.get("error_message", "") + nl + nl +
        "## 上一轮修复方案 (第 " + str(attempt) + " 次重试)" + nl +
        prev_fix_plan + nl + nl +
        "请重新分析并输出 JSON。"
    )

    try:
        response = client.messages.create(
            model=os.environ.get("CODEFIXER_CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        content = response.content[0].text if response.content else "{}"
        import re
        json_match = re.search(r"\{.*\}", content, re.DOTALL)
        analysis = json.loads(json_match.group(0)) if json_match else {}

        return {
            "root_cause": analysis.get("root_cause", ""),
            "fix_plan": analysis.get("fix_plan", ""),
            "test_cases": analysis.get("test_cases", []),
            "line_numbers": analysis.get("line_numbers", []),
            "input_tokens": response.usage.input_tokens if response.usage else 0,
            "output_tokens": response.usage.output_tokens if response.usage else 0,
        }
    except Exception as e:
        return {
            "root_cause": "API 调用失败",
            "fix_plan": prev_fix_plan,
            "test_cases": [],
            "line_numbers": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "error": str(e),
        }


def _extract_test_code(codex_logs, language):
    """从 Codex 测试结果中提取测试代码。"""
    if not codex_logs:
        return ""
    import re
    code_blocks = re.findall(
        r"```(?:python)?\s*\n(.*?)```", codex_logs, re.DOTALL
    )
    if code_blocks:
        return chr(10).join(code_blocks)
    return codex_logs


def _get_fixed_path(original_path):
    """生成 _fixed.py 路径 (不覆盖原文件)。"""
    stem = original_path.stem
    suffix = original_path.suffix
    return original_path.parent / (stem + "_fixed" + suffix)


# ═══════════════════════════════════════════════════════════════
# 兼容接口
# ═══════════════════════════════════════════════════════════════

def fix_file(file_path, analysis, cost_tracker, session_mgr,
             auto_confirm=False):
    """兼容旧接口 — 执行阶段 1 (prepare)。"""
    budget_check = cost_tracker.check_tier2_budget(file_path)
    if not budget_check["allowed"]:
        return {"status": "skipped", "reason": budget_check["reason"]}
    return prepare_codex_task(
        file_path, analysis, cost_tracker, session_mgr,
    )


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="code_fixer Tier 2")
    parser.add_argument("--file", type=str, required=True)
    parser.add_argument("--analysis", type=str, default=None)
    parser.add_argument("--phase", type=str, default="prepare",
                        choices=["prepare", "verify"])
    args = parser.parse_args()

    cost_tracker = CostTracker()
    session_mgr = SessionManager()
    session_mgr.init_code_fixer_dir()

    if args.phase == "prepare":
        if not args.analysis:
            print("错误: prepare 阶段需要 --analysis 参数")
            sys.exit(1)
        with open(args.analysis, "r", encoding="utf-8") as f:
            analysis = json.load(f)
        result = prepare_codex_task(
            args.file, analysis, cost_tracker, session_mgr,
        )
    else:
        result = verify_codex_result(args.file, cost_tracker, session_mgr)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
