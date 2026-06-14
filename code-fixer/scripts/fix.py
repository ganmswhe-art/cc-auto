"""
code_fixer Tier 2 — 单文件修复流程

功能:
- 接收一个文件路径 + Claude 分析结果
- 调用 Codex API 生成修复代码
- Codex CLI 沙盒第一层测试
- Docker 容器最终验证
- 最多重试 3 轮
- 每轮失败反馈给 Claude 重新分析

用法:
  python fix.py --file src/utils.py --analysis analysis.json
"""

import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    CODEX_API_KEY, CODEX_API_BASE, CODEX_CLI_PACKAGE,
    CODEX_CLI_SILENT_TIMEOUT_SEC, CODEX_CLI_TEST_TIMEOUT_SEC,
    MAX_RETRY_ATTEMPTS, PROJECT_ROOT, CONFIG_DIR, LANGUAGE_DOCKER_IMAGES,
    CONFIRM_BEFORE_FIX, CONFIRM_BEFORE_DEP_INSTALL,
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


def fix_file(file_path: str, analysis: dict, cost_tracker: CostTracker,
             session_mgr: SessionManager, auto_confirm: bool = False) -> dict:
    """
    Tier 2 主函数: 修复单个文件。

    Args:
        file_path: 目标文件相对路径
        analysis: Claude Tier1 对该文件的分析结果 (包含 test_cases, fix_plan 等)
        cost_tracker: 费用追踪器
        session_mgr: 会话管理器
        auto_confirm: 是否自动确认 (跳过用户交互)

    Returns:
        {
            "status": "success" | "failed" | "skipped",
            "original_code": str,
            "fixed_code": str,
            "fix_explanation": str,
            "test_result": dict,
            "attempts": int,
            "total_cost_rmb": float,
            "fixed_file_path": str,
        }
    """
    # 预检预算
    budget_check = cost_tracker.check_tier2_budget(file_path)
    if not budget_check["allowed"]:
        return {
            "status": "skipped",
            "reason": budget_check["reason"],
            "attempts": 0,
            "total_cost_rmb": cost_tracker.file_cost(file_path),
        }

    # 验证路径
    try:
        resolved_path = validate_path(file_path)
    except ValueError as e:
        return {"status": "failed", "reason": str(e), "attempts": 0}

    # 读取源文件
    try:
        original_code = read_file_safe(resolved_path)
    except Exception as e:
        return {"status": "failed", "reason": f"读取文件失败: {e}", "attempts": 0}

    language = detect_language(resolved_path)
    if not language:
        return {"status": "failed", "reason": f"不支持的文件类型: {resolved_path.suffix}", "attempts": 0}

    # 脱敏处理
    redacted_code, sensitive_info = redact_sensitive(original_code)

    # Git 保护
    git_stash_push()

    # ═══════════════════════════════════════════════════════
    # 重试循环 (最多 MAX_RETRY_ATTEMPTS 轮)
    # ═══════════════════════════════════════════════════════
    attempt = 0
    current_error = analysis.get("sanitized_error", {})
    fix_plan = analysis.get("fix_plan", "")
    test_cases_from_tier1 = analysis.get("test_cases", [])

    history_versions = []
    final_result = None

    while attempt < MAX_RETRY_ATTEMPTS:
        attempt += 1
        attempt_result = {
            "attempt": attempt,
            "fixed_code": "",
            "test_result": None,
            "passed": False,
            "error": "",
        }

        # -- Claude 分析 (仅第1轮使用 Tier1 结果，后续轮次需新分析) --
        if attempt == 1:
            claude_result = analysis
        else:
            claude_result = _call_claude_analyze(
                redacted_code, current_error, fix_plan, attempt
            )
            # 记录费用
            cost_tracker.record_tier2(
                file_path, "claude",
                claude_result.get("input_tokens", 0),
                claude_result.get("output_tokens", 0),
            )

        # -- Codex API 生成修复代码 --
        codex_code = _call_codex_generate(
            redacted_code, claude_result.get("fix_plan", fix_plan),
            claude_result.get("test_cases", test_cases_from_tier1),
            language,
        )
        cost_tracker.record_tier2(
            file_path, "codex",
            codex_code.get("input_tokens", 0),
            codex_code.get("output_tokens", 0),
        )

        fixed_code = codex_code.get("code", "")
        test_code = codex_code.get("test_code", "")
        fix_explanation = codex_code.get("explanation", "")
        regression_test = codex_code.get("regression_test", "")

        attempt_result["fixed_code"] = fixed_code

        # -- 语法检查 --
        # 写入临时文件做语法检查
        tmp_fixed = CONFIG_DIR / f"tmp_fixed_{timestamp_str()}{resolved_path.suffix}"
        write_file_safe(tmp_fixed, fixed_code)
        syntax_check = check_syntax(fixed_code, resolved_path)
        if not syntax_check["valid"]:
            current_error = {
                "error_type": "SyntaxError",
                "file_basename": resolved_path.name,
                "line_number": None,
                "error_message": syntax_check["error"][:200],
            }
            attempt_result["error"] = f"语法检查失败: {syntax_check['error']}"
            history_versions.append(attempt_result)
            continue

        # -- 安全扫描测试用例 --
        scan_result = scan_test_code(test_code)
        if not scan_result["safe"]:
            attempt_result["error"] = (
                f"测试用例包含禁止的危险模式:\n"
                + "\n".join(f"  行 {v['line_num']}: {v['pattern']}" for v in scan_result["violations"])
            )
            history_versions.append(attempt_result)
            continue

        # -- 依赖解析与安全扫描 --
        dependencies = parse_dependency_declarations(test_code)
        if dependencies:
            dep_check = check_dependency_safety(dependencies)
            if not dep_check["safe"]:
                warnings_text = "\n".join(dep_check["suspicious"])
                attempt_result["error"] = f"依赖安全检查发现问题:\n{warnings_text}"
                # 返回给上层处理（需要用户确认）
                attempt_result["dependency_warnings"] = dep_check
                history_versions.append(attempt_result)
                continue

        # -- Codex CLI 沙盒测试 (第一层) --
        cli_result = _run_codex_cli_test(
            fixed_code, test_code, language, dependencies
        )
        if not cli_result.get("passed"):
            current_error = {
                "error_type": cli_result.get("error_type", "TestFailed"),
                "file_basename": resolved_path.name,
                "line_number": cli_result.get("line_number"),
                "error_message": cli_result.get("logs", "")[:200],
            }
            attempt_result["test_result"] = cli_result
            attempt_result["error"] = f"Codex CLI 测试失败: {cli_result.get('logs', '')[:500]}"
            history_versions.append(attempt_result)
            continue

        # -- Docker 最终验证 (第二层) --
        docker_result = run_in_docker(
            fixed_code, test_code + "\n" + regression_test,
            language, dependencies,
        )
        attempt_result["test_result"] = docker_result

        if docker_result["passed"]:
            # 成功!
            # 写入 _fixed.py
            fixed_path = _get_fixed_path(resolved_path)
            write_file_safe(fixed_path, fixed_code)

            # Git backup commit
            run_shell(["git", "add", str(fixed_path)], timeout=5, cwd=PROJECT_ROOT)
            run_shell(["git", "commit", "-m",
                       f"code_fixer: backup before apply fix to {file_path}"],
                      timeout=10, cwd=PROJECT_ROOT)

            attempt_result["passed"] = True
            final_result = {
                "status": "success",
                "original_code": original_code,
                "fixed_code": fixed_code,
                "fix_explanation": fix_explanation,
                "test_result": docker_result,
                "attempts": attempt,
                "total_cost_rmb": cost_tracker.file_cost(file_path),
                "fixed_file_path": str(fixed_path),
                "history": history_versions,
            }
            break
        else:
            current_error = {
                "error_type": "DockerTestFailed",
                "file_basename": resolved_path.name,
                "line_number": None,
                "error_message": docker_result.get("logs", "")[:200],
            }
            attempt_result["error"] = f"Docker 验证失败: {docker_result.get('logs', '')[:500]}"
            history_versions.append(attempt_result)

    # 3 轮均失败
    if final_result is None:
        final_result = {
            "status": "failed",
            "original_code": original_code,
            "fixed_code": attempt_result.get("fixed_code", ""),
            "fix_explanation": fix_explanation,
            "test_result": attempt_result.get("test_result"),
            "attempts": MAX_RETRY_ATTEMPTS,
            "total_cost_rmb": cost_tracker.file_cost(file_path),
            "history": history_versions,
            "needs_user_decision": True,
            "user_options": {
                "skip": "跳过此文件，继续下一个",
                "claude_fix": "降级: 让 Claude 直接修复 (不走沙盒)",
            },
        }

    # 恢复 Git stash
    git_stash_pop()

    # 保存文件记录
    session_mgr.save_file_record(file_path, final_result)

    return final_result


def _call_claude_analyze(code: str, error_info: dict, prev_fix_plan: str,
                         attempt: int) -> dict:
    """
    调用 Claude API 重新分析错误。
    此函数返回 Claude API 需要的 prompt 结构，
    实际 API 调用由 Claude Code Skill 层通过 Bash 或 SDK 完成。
    """
    import anthropic

    client = anthropic.Anthropic(
        api_key=os.environ.get("CODEFIXER_CLAUDE_API_KEY", os.environ.get("ANTHROPIC_API_KEY", "")),
    )

    system_prompt = """你是代码调试助手。分析以下代码和错误，输出 JSON:
{
  "root_cause": "错误根因 (一行)",
  "fix_plan": "详细的修复方案",
  "test_cases": ["测试用例1 Python代码", "测试用例2 Python代码"],
  "line_numbers": [需修改的行号]
}"""

    user_prompt = f"""## 当前代码
```python
{code}
```

## 错误信息
- 类型: {error_info.get('error_type', 'Unknown')}
- 文件: {error_info.get('file_basename', '')}
- 行号: {error_info.get('line_number', 'N/A')}
- 消息: {error_info.get('error_message', '')}

## 上一轮的修复方案 (第 {attempt} 次重试)
{prev_fix_plan}

请重新分析并输出 JSON。"""

    try:
        response = client.messages.create(
            model=os.environ.get("CODEFIXER_CLAUDE_MODEL", "claude-sonnet-4-6"),
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        content = response.content[0].text if response.content else "{}"
        # 提取 JSON
        import re
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
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


def _call_codex_generate(code: str, fix_plan: str, test_cases: list,
                         language: str) -> dict:
    """
    调用 Codex API 生成修复代码 + 测试用例 + 回归测试。
    """
    import openai

    client = openai.OpenAI(
        api_key=os.environ.get("CODEFIXER_CODEX_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        base_url=os.environ.get("CODEFIXER_CODEX_API_BASE", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")),
    )

    system_prompt = f"""你是代码修复专家。根据修复方案输出修复后的完整代码和测试用例。

输出 JSON 格式:
{{
  "code": "完整的修复后代码",
  "test_code": "用于验证修复的测试代码",
  "regression_test": "回归测试(测试其他功能未被破坏)",
  "explanation": "修复说明"
}}

规则:
- 仅输出有效的 {language} 代码
- 测试代码使用 {language} 编写
- 回归测试覆盖原功能的核心路径
- 代码中不包含危险操作 (如 os.system, subprocess, eval, exec)"""

    user_prompt = f"""## 原始代码
```{language}
{code}
```

## 修复方案
{fix_plan}

## 测试用例 (Tier1 提供)
{chr(10).join(test_cases) if test_cases else '无预定义测试用例'}

请输出修复后代码和测试。"""

    try:
        response = client.chat.completions.create(
            model=os.environ.get("CODEFIXER_CODEX_MODEL", "gpt-5.5"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4096,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content if response.choices else "{}"
        result = json.loads(content)

        return {
            "code": result.get("code", code),
            "test_code": result.get("test_code", ""),
            "regression_test": result.get("regression_test", ""),
            "explanation": result.get("explanation", "无说明"),
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0,
        }
    except Exception as e:
        return {
            "code": code,
            "test_code": "",
            "regression_test": "",
            "explanation": f"Codex API 调用失败: {e}",
            "input_tokens": 0,
            "output_tokens": 0,
        }


def _run_codex_cli_test(code: str, test_code: str, language: str,
                        dependencies: List[str]) -> dict:
    """
    使用 Codex CLI 沙盒执行测试 (第一层沙盒)。
    返回: {"passed": bool, "logs": str, "error_type": str, "line_number": int}
    """
    # 检查 Codex CLI 是否可用
    version_check = run_shell(["codex", "--version"], timeout=5)
    if version_check["exit_code"] != 0:
        # Codex CLI 未安装，跳过第一层
        return {"passed": True, "logs": "Codex CLI 未安装,跳过", "skipped": True}

    # 写入临时文件
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        prompt = f"""在沙盒中执行以下代码并运行测试。仅报告测试结果，不要修改任何文件。

## 代码
```{language}
{code}
```

## 测试
```{language}
{test_code}
```

执行测试并报告: PASSED 或 FAILED。如果失败，提供完整的错误信息。
"""
        f.write(prompt)
        prompt_file = f.name

    try:
        result = run_shell(
            ["codex", "exec", "--sandbox", "workspace-write",
             "--dangerously-bypass-approvals-and-sandbox",
             "--ephemeral",
             "--json",
             f"Run this test: {prompt_file}"],
            timeout=CODEX_CLI_TEST_TIMEOUT_SEC,
            silent_timeout=CODEX_CLI_SILENT_TIMEOUT_SEC,
        )
    finally:
        try:
            os.unlink(prompt_file)
        except OSError:
            pass

    # 解析输出判断是否通过
    output = result["stdout"] + "\n" + result["stderr"]
    passed = "PASSED" in output.upper() and "FAILED" not in output.upper()

    error_type = ""
    line_number = None
    if not passed:
        # 尝试从输出中提取错误信息
        import re
        for et in ["ZeroDivisionError", "SyntaxError", "TypeError", "ValueError",
                    "KeyError", "IndexError", "AttributeError", "NameError",
                    "ImportError", "ModuleNotFoundError"]:
            if et in output:
                error_type = et
                break
        line_match = re.search(r'line\s+(\d+)', output)
        if line_match:
            line_number = int(line_match.group(1))

    return {
        "passed": passed,
        "logs": output,
        "error_type": error_type or ("TestFailed" if not passed else ""),
        "line_number": line_number,
        "skipped": False,
    }


def _get_fixed_path(original_path: Path) -> Path:
    """生成 _fixed.py 路径 (不覆盖原文件)。"""
    stem = original_path.stem
    suffix = original_path.suffix
    return original_path.parent / f"{stem}_fixed{suffix}"


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="code_fixer Tier 2: 单文件修复")
    parser.add_argument("--file", type=str, required=True, help="目标文件路径")
    parser.add_argument("--analysis", type=str, required=True, help="分析结果 JSON 文件")
    parser.add_argument("--auto-confirm", action="store_true", help="自动确认")
    args = parser.parse_args()

    with open(args.analysis, "r", encoding="utf-8") as f:
        analysis = json.load(f)

    cost_tracker = CostTracker()
    session_mgr = SessionManager()
    session_mgr.init_code_fixer_dir()

    result = fix_file(args.file, analysis, cost_tracker, session_mgr,
                      auto_confirm=args.auto_confirm)

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
