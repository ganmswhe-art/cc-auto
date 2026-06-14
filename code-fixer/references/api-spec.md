# code_fixer API 调用规范

## Claude API 调用

### 分析错误 (Tier1 + Tier2 重试)

```
POST {CLAUDE_API_BASE}/v1/messages
```

系统提示词:
```
你是代码调试助手。分析以下代码和错误，输出 JSON:
{
  "root_cause": "错误根因 (一行)",
  "fix_plan": "详细的修复方案",
  "test_cases": ["测试用例1 Python代码", "测试用例2 Python代码"],
  "line_numbers": [需修改的行号]
}
```

用户提示词模板:
```
## 当前代码
```{language}
{redacted_code}    ← 已自动脱敏
```

## 错误信息
- 类型: {error_type}        ← 本地脱敏后仅发送类型
- 行号: {line_number}
- 消息: {error_message}      ← 200 字符截断
```

### 注意事项
- 代码内容会预先脱敏（API Key/Token/密码替换为 [REDACTED]）
- 错误日志只在本地处理，API 仅收到脱敏摘要
- `max_tokens`: 2000
- 模型: 默认 `claude-sonnet-4-6`，可通过 `CODEFIXER_CLAUDE_MODEL` 环境变量覆盖

## Codex API 调用

### 生成修复代码 (Tier2)

```
POST {CODEX_API_BASE}/chat/completions
```

系统提示词:
```
你是代码修复专家。根据修复方案输出完整的修复后代码和测试用例。

输出 JSON 格式:
{
  "code": "完整的修复后代码",
  "test_code": "用于验证修复的测试代码",
  "regression_test": "回归测试(测试其他功能未被破坏)",
  "explanation": "修复说明"
}

规则:
- 仅输出有效的 {language} 代码
- 测试代码使用 {language} 编写
- 回归测试覆盖原功能的核心路径
- 代码中不包含危险操作 (os.system, subprocess, eval, exec)
```

### 注意事项
- `response_format`: `{"type": "json_object"}`
- `max_tokens`: 4096
- 模型: 默认 `gpt-5.5`，可通过 `CODEFIXER_CODEX_MODEL` 环境变量覆盖

## Codex CLI 调用

### 沙盒测试执行 (Tier2)

```bash
codex exec --sandbox workspace-write \
  --dangerously-bypass-approvals-and-sandbox \
  --ephemeral \
  --json \
  "Run this test: {prompt_file}"
```

- 静默超时: 60s (代码生成) / 120s (测试执行)
- Git stash 保护在此调用前执行
- 运行后检查非白名单文件变更，自动回滚

## 错误处理策略

| 错误类型 | 策略 |
|---------|------|
| API 超时 (>5s) | 重试 2 次，间隔 1s |
| 429 限流 | 指数退避: 5s → 10s → 20s... 最长 5 分钟 |
| 403 配额耗尽 | 保存进度，提示用户，可恢复 |
| 连接失败 | 重试 2 次 |

所有重试中保持进度状态，不可恢复错误保存断点。
