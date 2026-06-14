"""
code_fixer 费用追踪器

功能:
- 分层预算管理 (Tier1 全局分析 + Tier2 每个文件独立)
- Tier2 每个文件上限 ≤ Tier1 实际费用 × 40%
- 全局 10 元警告 + 20 元硬上限
- 每次超警告需用户确认
"""

from typing import Optional
from datetime import datetime, timezone

from config import (
    PRICING, GLOBAL_COST_WARNING_RMB, GLOBAL_COST_HARD_LIMIT_RMB,
)


class CostTracker:
    """费用追踪器（项目级单例）"""

    def __init__(self):
        self._tier1_cost: float = 0.0
        self._tier2_costs: dict = {}  # {file_path: total_cost}
        self._warnings_issued: int = 0
        self._hard_limit_reached: bool = False

    # ── 属性 ──────────────────────────────────────────────
    @property
    def tier1_cost(self) -> float:
        return self._tier1_cost

    @property
    def total_cost(self) -> float:
        return self._tier1_cost + sum(self._tier2_costs.values())

    @property
    def tier2_max_per_file(self) -> float:
        """Tier2 单文件上限 = Tier1 实际费 × 40%。Tier1 未执行时返回无限。"""
        if self._tier1_cost <= 0:
            return float("inf")
        return self._tier1_cost * 0.40

    @property
    def warning_triggered(self) -> bool:
        return self.total_cost >= GLOBAL_COST_WARNING_RMB

    @property
    def hard_limit_triggered(self) -> bool:
        return self.total_cost >= GLOBAL_COST_HARD_LIMIT_RMB

    # ── 费用计算 ──────────────────────────────────────────
    @staticmethod
    def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
        """计算单次 API 调用的费用（人民币元）。"""
        pricing = PRICING.get(model, PRICING["claude"])
        input_cost = (input_tokens / 1000) * pricing["input_per_1k"]
        output_cost = (output_tokens / 1000) * pricing["output_per_1k"]
        return round(input_cost + output_cost, 6)

    # ── 记录费用 ──────────────────────────────────────────
    def record_tier1(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """记录 Tier1 费用。"""
        cost = self.calculate_cost(model, input_tokens, output_tokens)
        self._tier1_cost += cost
        return cost

    def record_tier2(self, file_path: str, model: str,
                     input_tokens: int, output_tokens: int) -> float:
        """记录 Tier2 费用（按文件累加）。"""
        cost = self.calculate_cost(model, input_tokens, output_tokens)
        if file_path not in self._tier2_costs:
            self._tier2_costs[file_path] = 0.0
        self._tier2_costs[file_path] += cost
        return cost

    def file_cost(self, file_path: str) -> float:
        """查询某文件已花费的费用。"""
        return self._tier2_costs.get(file_path, 0.0)

    # ── 预算检查 ──────────────────────────────────────────
    def check_tier2_budget(self, file_path: str) -> dict:
        """
        检查 Tier2 单文件预算。
        返回: {"allowed": bool, "reason": str, "remaining": float}
        """
        spent = self.file_cost(file_path)
        max_allowed = self.tier2_max_per_file

        if max_allowed == float("inf"):
            return {"allowed": True, "reason": "Tier1 未执行，无上限", "remaining": float("inf")}

        remaining = max_allowed - spent
        if remaining <= 0:
            return {"allowed": False, "reason": f"文件 {file_path} 已超过单文件预算 {max_allowed:.3f} 元", "remaining": 0}

        return {"allowed": True, "reason": "", "remaining": remaining}

    def check_global_budget(self) -> dict:
        """
        检查全局预算。
        返回: {"warning": bool, "hard_stop": bool, "message": str}
        """
        total = self.total_cost
        result = {
            "warning": total >= GLOBAL_COST_WARNING_RMB,
            "hard_stop": total >= GLOBAL_COST_HARD_LIMIT_RMB,
            "message": "",
        }

        if result["hard_stop"]:
            result["message"] = (
                f"⚠️ 已达到全局硬上限 {GLOBAL_COST_HARD_LIMIT_RMB} 元"
                f"(当前: {total:.3f} 元)。流程已自动停止。"
            )
        elif result["warning"]:
            result["message"] = (
                f"⚠️ 当前项目累计费用已达 {total:.3f} 元"
                f"(警告阈值: {GLOBAL_COST_WARNING_RMB} 元)。"
            )

        return result

    # ── 状态序列化 ────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "tier1_cost": self._tier1_cost,
            "tier2_costs": self._tier2_costs,
            "total_cost": self.total_cost,
            "tier2_max_per_file": self.tier2_max_per_file,
            "warnings_issued": self._warnings_issued,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CostTracker":
        tracker = cls()
        tracker._tier1_cost = data.get("tier1_cost", 0.0)
        tracker._tier2_costs = data.get("tier2_costs", {})
        tracker._warnings_issued = data.get("warnings_issued", 0)
        return tracker
