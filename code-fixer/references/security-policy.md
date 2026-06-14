# code_fixer 安全策略

## 数据流安全

### 从用户到 API 的数据

```
用户提供的错误日志
    │
    ▼
[本地脱敏] ─ 提取错误类型 + 行号 + 200 字符截断
    │
    ▼
[格式白名单校验] ─ 仅放行已知异常类型
    │
    ▼
发送给 Claude API: {error_type, line_number, error_message[:200]}
```

### 代码内容

```
源代码文件
    │
    ▼
[高熵字符串检测] ─ API Key / Token / 密码 / Secret → [REDACTED]
    │
    ▼
[符号链接解析] ─ 确认在项目目录内
    │
    ▼
发送给 API
```

## 沙盒隔离层级

| 层 | 机制 | 限制 |
|---|------|------|
| 1 | Git stash 保护 | 源码可随时恢复 |
| 2 | Codex CLI --sandbox workspace-write | 进程级隔离 |
| 3 | Docker 容器 | 网络=none, 内存=256M, CPU=1, PID=50 |
| 4 | 写入 _fixed.py | 永不覆盖原文件 |

## 禁止的操作模式 (测试代码扫描)

- `os.system()`
- `subprocess.*`
- `eval()` / `exec()`
- `__import__()`
- `shutil.rmtree()`
- `os.remove()` / `os.unlink()` / `os.rmdir()`
- `importlib.import_module()`
- `compile(..., 'exec')`

## Prompt 注入防护

1. 错误日志脱敏时丢弃自由文本，仅保留结构化数据
2. 错误类型白名单 (仅 Python 标准异常)
3. 错误信息 200 字符硬截断
4. `shell=False` (参数化命令) 杜绝命令注入

## 文件系统安全

| 机制 | 说明 |
|------|------|
| 路径白名单 | `[a-zA-Z0-9_\-./:\\]` 仅允许安全字符 |
| Symlink 解析 | realpath 确认在项目目录内 |
| 写入隔离 | 始终写入 `_fixed.py` |
| Git 备份 | 每次写入前自动 commit |
| 文件锁 | 项目级互斥锁 + 30 分钟超时 |

## API Key 安全

- 从环境变量读取 (Claude Code settings.json env)
- 永不出现在日志中
- 使用完不写入任何文件

## 依赖安全

- 测试代码声明的依赖必须用户确认后安装
- PyPI 包名 typo-squatting 检测
- Docker 容器内安装，不影响宿主机

## 会话中断安全

- Ctrl+C → trap + atexit 清理 Docker/锁文件/Git stash
- 下次启动 → 冗余清理僵尸资源
- 断点 → 进度持久化到 session_state.json
