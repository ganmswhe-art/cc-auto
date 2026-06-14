# code_fixer — Claude + Codex 协作代码修复技能包

> 让 Claude 分析错误，让 Codex 生成修复并执行测试，双重沙盒验证后安全交付。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置 API Key

在 Claude Code 的 `settings.json` 中配置：

```json
{
  "env": {
    "CODEFIXER_CLAUDE_API_KEY": "sk-xxxx",
    "CODEFIXER_CODEX_API_KEY": "sk-xxxx"
  }
}
```

### 3. 环境检查

```bash
python scripts/skill.py --mode health --json
```

### 4. 使用

在 Claude Code 对话中直接说：

- "帮我修复这个项目的所有错误"
- "修一下 src/utils.py，报 ZeroDivisionError"
- "分析项目代码质量，修复问题"

## 架构

```
Tier 1 (Claude)                  Tier 2 (每个文件串行)
┌────────────────────┐           ┌──────────────────────────┐
│ 扫描项目所有代码   │           │ Claude 分析错误 → 修复方案│
│ 识别依赖关系       │──────→    │ Codex 生成修复代码 + 测试  │
│ 生成测试计划       │           │ 语法检查 → 安全扫描       │
│ test_plan.json    │           │ Codex CLI 沙盒测试        │
│ .test.md × N      │           │ Docker 容器最终验证       │
└────────────────────┘           │ 写入 _fixed.py           │
                                 └──────────────────────────┘
                                         重复最多 3 轮
```

## 安全机制

- ✅ Git 自动保护 (stash + diff + restore)
- ✅ 双重沙盒 (Codex CLI + Docker 断网)
- ✅ 敏感信息自动脱敏
- ✅ 错误日志本地脱敏后发送
- ✅ 写入 `_fixed.py`，从不覆盖原文件
- ✅ 项目级文件锁 + 超时过期

## 支持的平台与语言

| 平台 | 状态 |
|------|------|
| Windows | ✅ |
| macOS | ✅ |
| Linux | ✅ |

| 语言 | 默认支持 |
|------|---------|
| Python | ✅ |
| TypeScript/JavaScript | ✅ |
| Go | ✅ |
| 其他 | config.py 可扩展 |

## 目录结构

```
code_fixer/
├── SKILL.md              # Claude Code 入口
├── README.md
├── requirements.txt
├── scripts/
│   ├── skill.py          # 主编排器
│   ├── analyze.py        # Tier 1: 项目分析
│   ├── fix.py            # Tier 2: 单文件修复
│   ├── config.py         # 配置管理
│   ├── cost_tracker.py   # 费用追踪
│   ├── code_scanner.py   # 安全扫描
│   ├── docker_sandbox.py # Docker 沙盒
│   ├── session_manager.py# 会话管理
│   └── utils.py          # 工具函数
├── references/
│   ├── api-spec.md       # API 调用规范
│   └── security-policy.md# 安全策略
└── history/              # 运行历史 (自动生成)
```

## 许可

MIT License
