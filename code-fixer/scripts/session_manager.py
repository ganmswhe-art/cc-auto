"""
code_fixer 会话管理器

功能:
- 会话状态持久化与恢复
- 文件锁(项目级 + 超时过期)
- 历史记录管理 (保留最近 5 次)
"""

import os
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict

from config import (
    CONFIG_DIR, HISTORY_DIR, LOCK_TIMEOUT_MINUTES,
    SINGLE_FILE_HISTORY_MAX, PROJECT_ROOT,
)
from utils import timestamp_str


class SessionManager:
    """会话状态管理器"""

    def __init__(self):
        self.session_id = timestamp_str()
        self.session_dir: Optional[Path] = None
        self._lock_file = PROJECT_ROOT / ".code_fixer.lock"

    # ═══════════════════════════════════════════════════════
    # 锁管理
    # ═══════════════════════════════════════════════════════

    def acquire_lock(self) -> dict:
        """
        尝试获取项目级文件锁。
        返回: {"acquired": bool, "reason": str}
        """
        if self._lock_file.exists():
            try:
                lock_data = json.loads(self._lock_file.read_text())
                lock_time_str = lock_data.get("timestamp", "")
                lock_time = datetime.fromisoformat(lock_time_str)
                age = datetime.now(timezone.utc) - lock_time

                if age > timedelta(minutes=LOCK_TIMEOUT_MINUTES):
                    # 锁已过期，清理后重新获取
                    self._lock_file.unlink(missing_ok=True)
                else:
                    return {
                        "acquired": False,
                        "reason": (
                            f"项目 {PROJECT_ROOT.name} 已有修复进程在运行"
                            f"(会话: {lock_data.get('session_id', 'unknown')},"
                            f" {int(age.total_seconds() / 60)} 分钟前启动)。"
                            f"超时释放需 {LOCK_TIMEOUT_MINUTES - int(age.total_seconds() / 60)} 分钟。"
                        ),
                    }
            except (json.JSONDecodeError, KeyError, ValueError):
                # 锁文件损坏，清理
                self._lock_file.unlink(missing_ok=True)

        # 写入锁文件
        lock_data = {
            "session_id": self.session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        self._lock_file.write_text(json.dumps(lock_data, indent=2))
        return {"acquired": True, "reason": ""}

    def release_lock(self):
        """释放文件锁。"""
        try:
            if self._lock_file.exists():
                saved = json.loads(self._lock_file.read_text())
                if saved.get("session_id") == self.session_id:
                    self._lock_file.unlink(missing_ok=True)
        except Exception:
            self._lock_file.unlink(missing_ok=True)

    @staticmethod
    def cleanup_stale_locks():
        """清理僵尸锁文件。"""
        lock_file = PROJECT_ROOT / ".code_fixer.lock"
        if lock_file.exists():
            try:
                lock_data = json.loads(lock_file.read_text())
                lock_time = datetime.fromisoformat(lock_data["timestamp"])
                age = datetime.now(timezone.utc) - lock_time
                if age > timedelta(minutes=LOCK_TIMEOUT_MINUTES):
                    lock_file.unlink(missing_ok=True)
            except Exception:
                lock_file.unlink(missing_ok=True)

    # ═══════════════════════════════════════════════════════
    # 会话状态
    # ═══════════════════════════════════════════════════════

    def init_session(self) -> Path:
        """初始化新会话目录。"""
        self.session_dir = HISTORY_DIR / f"session_{self.session_id}"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        return self.session_dir

    def save_session_state(self, state: dict):
        """保存当前会话状态 (用于中断恢复)。"""
        if not self.session_dir:
            self.init_session()

        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        state_file = self.session_dir / "session_state.json"
        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))

    def load_incomplete_sessions(self) -> List[dict]:
        """
        扫描未完成的会话。
        返回: [{"session_id": str, "state": dict, "dir": Path}]
        """
        incomplete = []
        if not HISTORY_DIR.exists():
            return incomplete

        for session_dir in sorted(HISTORY_DIR.glob("session_*"), reverse=True):
            state_file = session_dir / "session_state.json"
            if not state_file.exists():
                continue

            try:
                state = json.loads(state_file.read_text())
                if state.get("status") != "completed":
                    incomplete.append({
                        "session_id": session_dir.name.replace("session_", ""),
                        "state": state,
                        "dir": session_dir,
                    })
            except (json.JSONDecodeError, KeyError):
                continue

        return incomplete

    def mark_session_completed(self):
        """标记当前会话为完成。"""
        if self.session_dir and self.session_dir.exists():
            state_file = self.session_dir / "session_state.json"
            try:
                state = json.loads(state_file.read_text())
                state["status"] = "completed"
                state["completed_at"] = datetime.now(timezone.utc).isoformat()
                state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════
    # 历史管理
    # ═══════════════════════════════════════════════════════

    def save_artifact(self, name: str, content) -> Path:
        """保存会话产物。"""
        if not self.session_dir:
            self.init_session()

        path = self.session_dir / name
        if isinstance(content, (dict, list)):
            path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_text(str(content), encoding="utf-8")
        return path

    def rotate_old_sessions(self, keep: int = SINGLE_FILE_HISTORY_MAX):
        """清理旧会话，只保留最近 N 个。"""
        if not HISTORY_DIR.exists():
            return

        sessions = sorted(HISTORY_DIR.glob("session_*"), reverse=True)
        for old_dir in sessions[keep:]:
            try:
                import shutil
                shutil.rmtree(old_dir, ignore_errors=True)
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════
    # .code_fixer 目录初始化
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def init_code_fixer_dir():
        """初始化项目级 .code_fixer 目录。确保有 .gitignore。"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

        gitignore_path = CONFIG_DIR / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n", encoding="utf-8")

    @staticmethod
    def save_test_plan(test_plan: dict):
        """保存测试计划到 .code_fixer/test_plan.json。"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        plan_path = CONFIG_DIR / "test_plan.json"
        plan_path.write_text(
            json.dumps(test_plan, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def load_test_plan() -> Optional[dict]:
        """加载测试计划。"""
        plan_path = CONFIG_DIR / "test_plan.json"
        if plan_path.exists():
            return json.loads(plan_path.read_text(encoding="utf-8"))
        return None

    @staticmethod
    def save_file_record(file_path: str, record: dict):
        """保存单个文件的修复记录。"""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # 文件路径转换为安全的文件名
        safe_name = file_path.replace("/", "_").replace("\\", "_").replace(":", "_")
        record_path = CONFIG_DIR / f"{safe_name}.json"
        record_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
