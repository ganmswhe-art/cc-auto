---
name: code-fixer
description: >-
  Automated code fixing using Claude + Codex collaboration with dual sandbox
  verification. Invoke when the user needs to fix bugs, repair code errors,
  debug issues, resolve test failures, or auto-repair broken code. Supports
  any mentions of: error traceback, test failure, bug fix, code repair,
  debug, fixing code, broken code, runtime error, syntax error, test this,
  code review with fixes. Two-tier architecture: Tier 1 Claude analyzes the
  entire project and writes test plans, Tier 2 each file gets serial
  fix→test→retry loops with Codex CLI sandbox + Docker verification.

compatibility: "Claude Code >=2.0, Codex CLI >=0.100, Docker >=24, Python >=3.9"
metadata:
  author: "code_fixer"
  version: "1.0.0"
  license: "MIT"
  requirements: "Docker, Git, Python 3.9+, Codex CLI (@openai/codex@0)"
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
arguments:
  - name: file
    description: "Target code file to fix"
    required: false
  - name: error
    description: "Error log or traceback"
    required: false
argument-hint: "[file] [error]"
---

# code_fixer — Claude + Codex 协作代码修复技能

## 概述

code_fixer 是一个双 AI 协作的技能包：
- **Claude** 负责错误分析、根因定位、修复方案制定
- **Codex** 负责修复代码生成、测试用例编写、沙盒测试执行
- **Docker** 提供最终验证沙盒

所有修复代码先验证再写入，不会污染用户的源代码。

## 何时激活

在以下任何场景下自动激活：
- 用户提到 "修复"、"改 bug"、"报错"、"调试"、"修代码"、"跑不起来"
- 对话中存在 traceback/错误日志
- 用户说 "帮我测试"、"代码审查"、"检查问题"、"帮我改这段代码"
- 模糊指令如 "这个函数对吗"、"有问题" 也激活，进入分析后再确认

## 执行流程

### 完整流程 (用户说 "修复项目里的所有问题")

```
步骤 0 — 环境健康检查
  → 运行: python scripts/skill.py --mode health --json
  → 检查: Claude API Key, Codex API Key, Docker, Git, Codex CLI
  → 任何失败: 输出明确错误 + 修复指引，停止流程
  → Codex CLI 未安装: 自动运行 npm install -g @openai/codex@0

步骤 1 — Tier 1: Claude 项目全局分析
  → 运行: python scripts/skill.py --mode tier1 --json
  → Claude 扫描所有代码文件（跳过 node_modules, .git, venv 等）
  → 输出: test_plan.json + 每个文件 .test.md
  → 展示分析报告（文件数、语言、估算费用）
  → 等待用户确认: "是否继续进入逐文件修复？"

步骤 2 — Tier 2: 逐文件修复 (确认后自动运行)
  → 运行: python scripts/skill.py --mode tier2 --file <文件路径>
  → 每个文件执行:
      a) Claude 分析错误 → 输出根因 + 修复方案
      b) Codex API 生成修复代码 + 测试用例 + 回归测试
      c) 语法检查 (python -m py_compile / node --check)
      d) 安全扫描 (禁止 os.system, eval 等)
      e) Codex CLI 沙盒测试 (第一层)
      f) Docker 容器验证 (第二层，断网 + 资源限制)
      g) 通过 → 写入 _fixed.py (不覆盖原文件)
      h) 失败 → 回到 a) 重试，最多 3 轮
      i) 3 轮全失败 → 暂停，询问用户 (跳过 / Claude直接修复)

步骤 3 — 汇总报告
  → 展示: 通过/失败/跳过 文件数、总费用
  → 已完成会话保存到 history/
```

### 单文件修复 (用户说 "修一下 src/utils.py 这个文件")

```
1. 先运行 Tier1 快速分析（如果尚未分析该文件）
2. 执行 Tier2 针对该文件的修复流程
3. 每轮结果展示给用户
```

## 关键命令

### 环境检查
```bash
cd <skill目录>
python scripts/skill.py --mode health --json
```

### 完整修复
```bash
cd <skill目录>
python scripts/skill.py --mode auto --json
```

### 仅分析 (不执行修复)
```bash
cd <skill目录>
python scripts/skill.py --mode tier1 --json
```

### 恢复中断的会话
```bash
cd <skill目录>
python scripts/skill.py --mode resume --json
```

## 用户交互协议

### 确认点 1: Tier1 分析后
向用户展示分析结果后，**必须**询问：
```
> 分析完成。发现 N 个文件需要修复，估算费用 X 元。
> 是否继续逐文件修复？(继续/跳过某些文件/取消)
```

### 确认点 2: 3 轮重试失败后
```
> ❌ src/utils.py 已重试 3 次仍未通过测试。
> 选择: (s) 跳过此文件，继续下一个  (c) 让 Claude 直接修复此文件
```

### 确认点 3: 依赖安装
发现测试代码声明了第三方依赖时：
```
> ⚠️ 测试代码需要安装以下依赖: pytest, numpy
> 是否安装？(是/否/更改依赖)
```

### 确认点 4: 费用警告
累计费用超过 10 元时：
```
> ⚠️ 当前累计费用 11.5 元 (阈值 10 元)。回复"继续"确认继续。
```

### 对话打断
用户任何时候说 "跳过"、"下一个"、"停止"、"取消" 均应立即响应。

## 安全机制 (不可跳过)

1. **路径校验**: 只接受白名单字符 `[a-zA-Z0-9_\-./:\\]`
2. **符号链接解析**: 解析 realpath，拒绝项目外路径
3. **敏感信息脱敏**: 代码中的 API Key/Token/密码 自动替换为 [REDACTED] 后发送
4. **错误日志脱敏**: 只发送错误类型+行号+200字符截断摘要给 Claude
5. **Prompt 注入防护**: 白名单错误类型 + 200 字符硬截断
6. **Git 自动保护**: 每次 Codex CLI 运行前 `git stash`，非白名单文件改动自动回滚
7. **双重沙盒**: Codex CLI 沙盒 + Docker 容器 (断网 + 内存256M + PID限制50)
8. **测试代码扫描**: 禁止 os.system/subprocess/eval/exec/__import__/shutil.rmtree
9. **写入保护**: 始终写入 `_fixed.py`，从不覆盖原文件
10. **项目级锁**: `.code_fixer.lock` 文件，超时 30 分钟自动过期

## 费用控制

| 层级 | 规则 |
|------|------|
| Tier1 | 全局分析，按实际消费 |
| Tier2 每文件 | 上限 = Tier1 实际费 × 40% |
| 全局警告 | 累计 10 元时警告 |
| 全局硬上限 | 累计 20 元时自动停止 |
| 超出文件预算 | 仅跳过该文件，不影响其他文件 |

## 配置

所有 API Key 通过 Claude Code settings.json 的 env 配置:

```json
{
  "env": {
    "CODEFIXER_CLAUDE_API_KEY": "your-api-key",
    "CODEFIXER_CLAUDE_API_BASE": "https://api.deepseek.com/anthropic",
    "CODEFIXER_CODEX_API_KEY": "your-api-key",
    "CODEFIXER_CODEX_API_BASE": "https://api.deepseek.com/v1"
  }
}
```

如果未设置 `CODEFIXER_*` 变量，回退到 `ANTHROPIC_API_KEY` 和 `OPENAI_API_KEY`。

## 依赖安装

```bash
cd <skill目录>
pip install -r requirements.txt
```

## 注意事项

- 所有修复代码会生成 `文件名_fixed.py`，不会覆盖原始文件
- `.code_fixer/` 目录包含所有中间产物和记录，建议加入 `.gitignore`
- 历史记录默认保留最近 5 次会话，旧会话自动清理
- Docker 必须预先安装并运行（技能包提供检测但不安装 Docker）
- 仅支持 Python / TypeScript+JavaScript / Go，其他语言可通过 config.py 扩展
