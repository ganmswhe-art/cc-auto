"""
code_fixer Tier 1 — Claude 项目全局分析

输入: 项目目录
输出: test_plan.json + 每个文件 .test.md
      第一级分析报告 (费用、识别到的文件、测试计划)

用法:
  python analyze.py
  python analyze.py --project /path/to/project
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict

# 将 scripts 目录加入路径
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    CLAUDE_API_KEY, CLAUDE_API_BASE, PROJECT_ROOT,
    LANGUAGE_DOCKER_IMAGES, CONFIG_DIR,
)
from utils import (
    scan_project_code_files, detect_language, read_file_safe,
    redact_sensitive, timestamp_str,
)
from cost_tracker import CostTracker
from session_manager import SessionManager


def analyze_project(project_path: Path) -> dict:
    """
    Tier 1 主函数: 分析项目，为每个文件生成测试计划。

    返回:
    {
        "status": "success" | "failed",
        "test_plan": {...},
        "cost": float,
        "files_analyzed": int,
        "analysis_report": str,
    }
    """
    cost_tracker = CostTracker()
    session_mgr = SessionManager()
    session_mgr.init_code_fixer_dir()

    # 1. 扫描代码文件
    code_files = scan_project_code_files()
    if not code_files:
        return {
            "status": "failed",
            "error": "项目中未找到支持的代码文件",
            "files_analyzed": 0,
            "cost": 0,
        }

    # 2. 按语言分组
    files_by_lang: Dict[str, List[Path]] = {}
    for f in code_files:
        lang = detect_language(f)
        if lang:
            files_by_lang.setdefault(lang, []).append(f)

    # 3. 为每个文件生成测试计划
    #    这里通过 Claude Code 的 Skill 机制来调用 Claude API
    #    实际 API 调用由 Claude Code 环境处理
    #    analyze.py 输出 JSON 供 Claude Code 消费

    test_plan = {
        "project": str(project_path),
        "generated_at": timestamp_str(),
        "total_files": len(code_files),
        "languages": list(files_by_lang.keys()),
        "files": {},
        "cost_estimate": _estimate_analysis_cost(code_files),
    }

    for f in code_files:
        lang = detect_language(f)
        rel_path = str(f.relative_to(project_path))

        # 读取并脱敏代码
        try:
            code = read_file_safe(f)
            redacted_code, found_sensitive = redact_sensitive(code)
        except Exception as e:
            test_plan["files"][rel_path] = {
                "status": "read_error",
                "error": str(e),
            }
            continue

        test_plan["files"][rel_path] = {
            "language": lang,
            "size_lines": len(code.split("\n")),
            "size_bytes": f.stat().st_size,
            "has_sensitive_info": bool(found_sensitive),
            "sensitive_types": found_sensitive,
            "test_cases": [],           # Claude 填充
            "dependencies": [],          # 依赖的其他文件
            "status": "pending_analysis",
        }

        # 生成 .test.md (每个文件的测试规格)
        _generate_test_md(rel_path, f, lang, redacted_code, project_path)

    # 4. 保存测试计划
    test_plan["status"] = "ready"
    session_mgr.save_test_plan(test_plan)

    # 5. 保存状态（支持中断恢复）
    session_mgr.init_session()
    session_mgr.save_session_state({
        "stage": "tier1_complete",
        "test_plan": test_plan,
        "cost_tracker": cost_tracker.to_dict(),
        "status": "in_progress",
    })

    return {
        "status": "success",
        "test_plan": test_plan,
        "cost": cost_tracker.total_cost,
        "files_analyzed": len(code_files),
        "analysis_report": _format_report(test_plan),
    }


def _estimate_analysis_cost(code_files: List[Path]) -> dict:
    """估算 Tier1 分析费用。"""
    total_lines = 0
    for f in code_files:
        try:
            total_lines += len(read_file_safe(f).split("\n"))
        except Exception:
            total_lines += 50  # 估算

    est_tokens = total_lines * 2  # 粗略估计每行代码 ≈ 2 token
    est_input_cost = (est_tokens / 1000) * 0.014  # DeepSeek V4 Pro 输入价
    est_output_cost = (est_tokens * 0.3 / 1000) * 0.028  # 输出约为输入 30%

    return {
        "total_files": len(code_files),
        "total_lines": total_lines,
        "est_tokens": est_tokens,
        "est_cost_rmb": round(est_input_cost + est_output_cost, 3),
    }


def _generate_test_md(rel_path: str, full_path: Path, language: str,
                      redacted_code: str, project_path: Path):
    """为单个文件生成 .test.md。"""
    safe_name = rel_path.replace("/", "_").replace("\\", "_")
    md_path = CONFIG_DIR / f"{safe_name}.test.md"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lang_cfg = LANGUAGE_DOCKER_IMAGES.get(language, {})
    ext = lang_cfg.get("extensions", [".py"])[0]

    content = f"""# 测试规格: {rel_path}

## 文件信息
- **路径**: {rel_path}
- **语言**: {language}
- **大小**: {full_path.stat().st_size:,} 字节

## 测试用例
<!-- Claude 将在此处填写测试用例 -->

## 依赖
<!-- 此文件依赖的项目内其他文件 -->

## Docker 配置
- **镜像**: {lang_cfg.get('image', 'N/A')}
- **语法检查**: `{lang_cfg.get('check_cmd', 'N/A')}`
- **测试命令**: `{lang_cfg.get('test_cmd', 'N/A')}`
"""
    md_path.write_text(content, encoding="utf-8")


def _format_report(test_plan: dict) -> str:
    """格式化 Tier1 分析报告（人类可读）。"""
    lines = [
        "=" * 60,
        "  code_fixer Tier 1 — 项目分析报告",
        "=" * 60,
        f"项目: {test_plan['project']}",
        f"文件总数: {test_plan['total_files']}",
        f"语言: {', '.join(test_plan['languages'])}",
        f"估算费用: {test_plan.get('cost_estimate', {}).get('est_cost_rmb', 'N/A')} 元",
        "",
        "各文件状态:",
    ]

    for path, info in test_plan["files"].items():
        status_icon = {"pending_analysis": "⏳", "read_error": "❌", "ready": "✅"}.get(
            info.get("status", ""), "❓"
        )
        lines.append(f"  {status_icon} {path} ({info.get('language', '?')}, {info.get('size_lines', 0)} 行)")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="code_fixer Tier 1: 项目分析")
    parser.add_argument("--project", type=str, default=None, help="项目路径 (默认当前目录)")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    args = parser.parse_args()

    project_path = Path(args.project).resolve() if args.project else PROJECT_ROOT

    result = analyze_project(project_path)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result.get("analysis_report", json.dumps(result, indent=2, ensure_ascii=False)))


if __name__ == "__main__":
    main()
