# cc-auto — Claude + Codex 自动化协作技能仓库

让 Claude 分析错误，让 Codex 生成修复代码并执行沙盒测试，双重验证后安全交付。

## 包含的技能

| 技能 | 说明 |
|------|------|
| [code-fixer](./code-fixer/) | 自动代码修复：Claude 分析错误 + Codex 生成修复 + 双重沙盒验证 |

## 安装

1. 在 CC Switch 中添加此仓库：`ganmswhe-art/cc-auto`（分支 `master`）
2. 在 Skills 标签页找到 `code-fixer`，点击安装

## 要求

- Claude Code >= 2.0
- Codex CLI >= 0.100
- Docker >= 24
- Python >= 3.9
