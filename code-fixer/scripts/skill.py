"""
code_fixer 主编排器

两层架构:
  Tier 1: Claude 全局分析 ─→ test_plan.json + .test.md
  Tier 2: 每个代码文件串行修复 ─→ _fixed.py + 验证结果

用法 (由 Claude Code SKILL.md 触发):
  python scripts/skill.py --mode auto
  python scripts/skill.py --mode tier1
  python scripts/skill.py --mode tier2 --file src/utils.py
  python scripts/skill.py --mode resume
  python scripts/skill.py --mode install-check
  python scripts/skill.py --mode health
"""

import os
import sys
import json
import time
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    CLAUDE_API_KEY, CODEX_API_KEY, PROJECT_ROOT, CONFIG_DIR, HISTORY_DIR,
    LANGUAGE_DOCKER_IMAGES, MAX_RETRY_ATTEMPTS, GLOBAL_COST_WARNING_RMB,
    GLOBAL_COST_HARD_LIMIT_RMB, CODEX_CLI_PACKAGE, LOCK_TIMEOUT_MINUTES,
    IS_WINDOWS, IS_MACOS, IS_LINUX,
)
from utils import (
    validate_path, scan_project_code_files, detect_language,
    read_file_safe, sanitize_error_log, redact_sensitive,
    run_shell, setup_signal_handlers, register_cleanup,
    is_git_repo, has_uncommitted_changes, init_git_repo,
    timestamp_str, format_duration_ms,
)
from cost_tracker import CostTracker
from session_manager import SessionManager
from docker_sandbox import (
    check_docker_installed, check_docker_running, check_docker_mount,
    cleanup_docker_resources,
)
from analyze import analyze_project
from fix import fix_file


def run_health_check() -> dict:
    """启动健康检查。返回所有检查项的状态。"""
    result = {
        "timestamp": timestamp_str(),
        "platform": sys.platform,
        "checks": {},
        "all_pass": True,
        "errors": [],
        "warnings": [],
    }

    def check(name: str, fn, *args, error_msg: str = ""):
        try:
            ok = fn(*args)
            result["checks"][name] = ok
            if not ok and error_msg:
                result["errors"].append(f"[{name}] {error_msg}")
                result["all_pass"] = False
            return ok
        except Exception as e:
            result["checks"][name] = False
            result["errors"].append(f"[{name}] 异常: {e}")
            result["all_pass"] = False
            return False

    # Claude API Key
    check("claude_api_key", lambda: bool(CLAUDE_API_KEY),
          error_msg="CLAUDE_API_KEY 未设置。请在 Claude Code settings.json 的 env 中添加 CODEFIXER_CLAUDE_API_KEY")

    # Codex API Key
    check("codex_api_key", lambda: bool(CODEX_API_KEY),
          error_msg="CODEX_API_KEY 未设置。请在 Claude Code settings.json 的 env 中添加 CODEFIXER_CODEX_API_KEY")

    # Claude API 1-token ping
    if CLAUDE_API_KEY:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            resp = client.messages.create(
                model=os.environ.get("CODEFIXER_CLAUDE_MODEL", "claude-sonnet-4-6"),
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            check("claude_api_reachable", lambda: True)
        except Exception as e:
            check("claude_api_reachable", lambda: False,
                  error_msg=f"Claude API 不可达: {e}")

    # Codex API 1-token ping
    if CODEX_API_KEY:
        try:
            import openai
            client = openai.OpenAI(
                api_key=CODEX_API_KEY,
                base_url=os.environ.get("CODEFIXER_CODEX_API_BASE", os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")),
            )
            resp = client.chat.completions.create(
                model=os.environ.get("CODEFIXER_CODEX_MODEL", "gpt-5.5"),
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            check("codex_api_reachable", lambda: True)
        except Exception as e:
            check("codex_api_reachable", lambda: False,
                  error_msg=f"Codex API 不可达: {e}")

    # Git
    check("git_available", lambda: run_shell(["git", "--version"], timeout=5)["exit_code"] == 0,
          error_msg="Git 未安装或不在 PATH 中")

    # Docker
    docker_ok = check("docker_installed", check_docker_installed,
                       error_msg="Docker 未安装")
    if docker_ok:
        check("docker_running", check_docker_running,
              error_msg="Docker 未运行。请启动 Docker Desktop/Docker Engine")
        if check_docker_running():
            mount = check_docker_mount(PROJECT_ROOT)
            check("docker_mount", lambda: mount["ok"],
                  error_msg=f"Docker 挂载测试失败: {mount.get('error', '')}\n{mount.get('platform_hint', '')}")

    # Codex CLI
    codex_version = run_shell(["codex", "--version"], timeout=5)
    check("codex_cli_installed", lambda: codex_version["exit_code"] == 0,
          error_msg=f"Codex CLI 未安装。请运行: npm install -g {CODEX_CLI_PACKAGE}")

    # 项目目录
    check("project_dir", lambda: PROJECT_ROOT.exists(),
          error_msg=f"项目目录不存在: {PROJECT_ROOT}")

    return result


def run_tier1(project_path: Optional[Path] = None) -> dict:
    """
    执行 Tier 1: 项目全局分析。
    返回 test_plan 供后续 Tier 2 使用。
    """
    path = project_path or PROJECT_ROOT
    return analyze_project(path)


def run_tier2(test_plan: dict, cost_tracker: CostTracker,
              session_mgr: SessionManager) -> dict:
    """
    执行 Tier 2: 串行处理每个文件。
    """
    results = {
        "total_files": 0,
        "passed": [],
        "failed": [],
        "skipped": [],
        "total_cost_rmb": 0.0,
        "completed": False,
    }

    files = list(test_plan.get("files", {}).keys())
    results["total_files"] = len(files)

    if not files:
        results["completed"] = True
        return results

    for i, file_path in enumerate(files, 1):
        # 检查全局预算
        global_check = cost_tracker.check_global_budget()
        if global_check["hard_stop"]:
            print(f"\n{global_check['message']}")
            results["skipped"].extend(files[i-1:])
            break

        # 检查单文件预算
        budget_check = cost_tracker.check_tier2_budget(file_path)
        if not budget_check["allowed"]:
            print(f"\n⚠️ {budget_check['reason']} — 跳过")
            results["skipped"].append(file_path)
            continue

        # 获取该文件的 Tier1 分析
        file_analysis = test_plan["files"].get(file_path, {})

        print(f"\n[{i}/{len(files)}] 正在修复: {file_path}")

        try:
            result = fix_file(file_path, file_analysis, cost_tracker, session_mgr)
        except Exception as e:
            result = {
                "status": "failed",
                "reason": f"修复过程中异常: {e}",
                "attempts": 0,
            }

        if result["status"] == "success":
            results["passed"].append(file_path)
            results["total_cost_rmb"] += result.get("total_cost_rmb", 0)
            print(f"  ✅ 通过 (尝试 {result['attempts']} 次, 费用 {result.get('total_cost_rmb', 0):.3f} 元)")

        elif result["status"] == "failed":
            if result.get("needs_user_decision"):
                # 3轮全失败 — 询问用户
                print(f"  ❌ {file_path} 已重试 {MAX_RETRY_ATTEMPTS} 次仍失败")
                print(f"     选项: (s)跳过  (c)Claude直接修复")
                # 在 Claude Code 对话中，由 SKILL.md 处理交互
            results["failed"].append(file_path)
            results["total_cost_rmb"] += result.get("total_cost_rmb", 0)

        elif result["status"] == "skipped":
            results["skipped"].append(file_path)
            print(f"  ⏭️ 跳过: {result.get('reason', '')}")

        # 保存进度
        session_mgr.save_session_state({
            "stage": "tier2_in_progress",
            "current_index": i,
            "total_files": len(files),
            "results": results,
            "cost_tracker": cost_tracker.to_dict(),
            "status": "in_progress",
        })

    # 标记完成
    if len(results["passed"]) + len(results["failed"]) + len(results["skipped"]) == results["total_files"]:
        results["completed"] = True

    session_mgr.save_session_state({
        "stage": "tier2_complete",
        "results": results,
        "cost_tracker": cost_tracker.to_dict(),
        "status": "completed",
    })

    return results


def run_full_flow(project_path: Optional[Path] = None) -> dict:
    """执行完整的两层修复流程。"""
    setup_signal_handlers()

    session_mgr = SessionManager()
    cost_tracker = CostTracker()

    # 注册清理
    register_cleanup(session_mgr.release_lock)
    register_cleanup(cleanup_docker_resources)

    # 清理僵尸资源
    SessionManager.cleanup_stale_locks()
    cleanup_docker_resources()

    # ═══════════════════════════════════════════════════════
    # 启动检查
    # ═══════════════════════════════════════════════════════
    health = run_health_check()
    if not health["all_pass"]:
        return {
            "status": "error",
            "stage": "health_check",
            "errors": health["errors"],
            "message": "环境检查未通过。请修复以上问题后重试。",
        }

    # 检查未完成的会话
    incomplete = session_mgr.load_incomplete_sessions()
    if incomplete:
        return {
            "status": "paused",
            "stage": "resume_check",
            "incomplete_sessions": incomplete,
            "message": f"发现 {len(incomplete)} 个未完成的修复会话，是否恢复？",
        }

    # 锁检查
    lock_result = session_mgr.acquire_lock()
    if not lock_result["acquired"]:
        return {
            "status": "error",
            "stage": "lock_check",
            "message": lock_result["reason"],
        }

    # Git 检查
    if not is_git_repo():
        print("📦 项目不是 Git 仓库，正在自动初始化...")
        init_git_repo()
        print("✅ Git 仓库已初始化")

    if has_uncommitted_changes():
        return {
            "status": "paused",
            "stage": "git_check",
            "message": "检测到未提交的修改。请先提交或暂存你的修改 (git commit / git stash)，再运行修复流程。",
        }

    # ═══════════════════════════════════════════════════════
    # Tier 1: 项目分析
    # ═══════════════════════════════════════════════════════
    print("🔍 Tier 1: 正在分析项目...")
    tier1_result = run_tier1(project_path)

    if tier1_result["status"] != "success":
        return {
            "status": "error",
            "stage": "tier1",
            "message": tier1_result.get("error", "Tier1 分析失败"),
        }

    test_plan = tier1_result["test_plan"]
    print(tier1_result.get("analysis_report", ""))
    print(f"\n📋 测试计划已保存到: {CONFIG_DIR / 'test_plan.json'}")

    # 展示 Tier1 结果，等待用户确认后进入 Tier2
    print(f"\n💰 Tier1 费用: {cost_tracker.tier1_cost:.3f} 元")
    print(f"📊 每个文件 Tier2 预算上限: {cost_tracker.tier2_max_per_file:.3f} 元")

    # ═══════════════════════════════════════════════════════
    # Tier 2: 逐文件修复
    # ═══════════════════════════════════════════════════════
    tier2_result = run_tier2(test_plan, cost_tracker, session_mgr)

    # ═══════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════
    session_mgr.mark_session_completed()
    session_mgr.release_lock()
    session_mgr.rotate_old_sessions()

    summary = {
        "status": "success" if tier2_result.get("completed") else "partial",
        "tier1_cost": cost_tracker.tier1_cost,
        "tier2_total_cost": tier2_result.get("total_cost_rmb", 0),
        "total_cost": cost_tracker.total_cost,
        "files_passed": len(tier2_result.get("passed", [])),
        "files_failed": len(tier2_result.get("failed", [])),
        "files_skipped": len(tier2_result.get("skipped", [])),
        "files_total": tier2_result.get("total_files", 0),
        "tier2_detail": tier2_result,
    }

    _print_summary(summary)
    return summary


def _print_summary(summary: dict):
    """打印最终汇总。"""
    print("\n" + "=" * 60)
    print("  code_fixer 修复汇总")
    print("=" * 60)
    print(f"  通过: {summary['files_passed']}")
    print(f"  失败: {summary['files_failed']}")
    print(f"  跳过: {summary['files_skipped']}")
    print(f"  总计: {summary['files_total']}")
    print(f"  Tier1 费用: {summary['tier1_cost']:.3f} 元")
    print(f"  Tier2 费用: {summary['tier2_total_cost']:.3f} 元")
    print(f"  总费用: {summary['total_cost']:.3f} 元")

    failed = summary.get("tier2_detail", {}).get("failed", [])
    if failed:
        print(f"\n  ⚠️ 以下文件修复失败:")
        for f in failed:
            print(f"     - {f}")


def run_install_codex_cli():
    """自动安装 Codex CLI（锁定主版本）。"""
    print(f"📦 正在安装 Codex CLI ({CODEX_CLI_PACKAGE})...")

    # 先检查 npm 可用
    npm_check = run_shell(["npm", "--version"], timeout=5)
    if npm_check["exit_code"] != 0:
        return {"status": "error", "message": "npm 未安装。请先安装 Node.js (nodejs.org)"}

    result = run_shell(
        ["npm", "install", "-g", CODEX_CLI_PACKAGE],
        timeout=120,
    )

    if result["exit_code"] == 0:
        return {"status": "success", "message": "Codex CLI 安装成功"}
    else:
        return {
            "status": "error",
            "message": f"Codex CLI 安装失败: {result['stderr']}",
        }


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="code_fixer: Claude + Codex 协作代码修复")
    parser.add_argument("--mode", type=str, default="auto",
                        choices=["auto", "tier1", "tier2", "resume", "health", "install-codex", "install-check"],
                        help="运行模式")
    parser.add_argument("--file", type=str, default=None, help="单文件修复 (Tier2 模式)")
    parser.add_argument("--project", type=str, default=None, help="项目路径")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument("--session", type=str, default=None, help="恢复的会话 ID")

    args = parser.parse_args()

    if args.mode == "health":
        result = run_health_check()
    elif args.mode == "install-codex":
        result = run_install_codex_cli()
    elif args.mode == "install-check":
        health = run_health_check()
        result = {
            "ready": health["all_pass"],
            "checks": health["checks"],
            "errors": health["errors"],
        }
    elif args.mode == "tier1":
        project_path = Path(args.project) if args.project else None
        result = run_tier1(project_path)
    elif args.mode == "tier2":
        if not args.file:
            print("错误: Tier2 模式需要 --file 参数")
            sys.exit(1)
        session_mgr = SessionManager()
        cost_tracker = CostTracker()
        test_plan = session_mgr.load_test_plan() or {"files": {}}
        file_analysis = test_plan.get("files", {}).get(args.file, {})
        result = fix_file(args.file, file_analysis, cost_tracker, session_mgr)
    elif args.mode == "resume":
        session_mgr = SessionManager()
        incomplete = session_mgr.load_incomplete_sessions()
        if not incomplete:
            result = {"status": "info", "message": "没有未完成的会话"}
        else:
            result = {
                "status": "paused",
                "sessions": [
                    {"id": s["session_id"], "state": s["state"]}
                    for s in incomplete
                ],
            }
    else:  # auto
        project_path = Path(args.project) if args.project else None
        result = run_full_flow(project_path)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if isinstance(result, dict):
            if "message" in result:
                print(result["message"])
            elif "errors" in result:
                for e in result.get("errors", []):
                    print(f"❌ {e}")
        else:
            print(result)


if __name__ == "__main__":
    main()
