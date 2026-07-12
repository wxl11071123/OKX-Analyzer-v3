"""创建 Phase 1 定时任务：选币扫描 + 日报/周报/月报。

运行方式: python -m src.scheduled_research.setup_jobs
前置条件: VIBE_TRADING_ENABLE_SCHEDULER=1
"""

from __future__ import annotations

import logging
import time

from src.scheduled_research.models import ScheduledResearchJob, JobStatus
from src.scheduled_research.store import ScheduledResearchJobStore

logger = logging.getLogger(__name__)

# 定时 job（UTC 时间，北京时间 = UTC+8）
# 选币+AI评估: UTC 0:00 每天 = 北京每天 08:00
# 日报: UTC 14:00 = 北京每天 22:00
# 周报+AI分析: UTC 2:00 周一 = 北京周一 10:00
# 月报+AI分析: UTC 2:00 1号 = 北京 1号 10:00

JOBS = [
    {
        "id": "coin-scanner-daily",
        "prompt": (
            "你是 TSMOM 自动交易系统的选币 AI。你必须严格按照 JSON 格式输出结果。所有输出必须是合法的 JSON，不要添加任何 JSON 之外的文字。\n\n"
            "执行每日选币扫描，按以下步骤操作，不可跳过：\n\n"
            "步骤 1: 调用 coin_scanner(min_vol_24h=100000000, top_n=10) 获取候选列表。\n"
            "步骤 2: 提取返回 JSON 中的 candidates 字段，对每个候选评估非技术面因素：\n"
            "  - 近期是否有代币解锁/大额解锁事件\n"
            "  - 是否有黑客攻击/合约漏洞/退市风险\n"
            "  - 是否有重大治理变更或团队异动\n\n"
            "步骤 3: 以 JSON 格式输出评估结果（必须严格遵守以下格式）。\n"
            "步骤 4: 用 send_feishu_card 推送结果。\n\n"
            "JSON 输出格式（必须严格遵守）：\n"
            '{"status":"ok","selected":[{"symbol":"XXX-USDT-SWAP","direction":"long","price":0.0,"tsmom_pct":0.0,"hurst":0.0,"reason":"技术面强劲无风险事件"}],'
            '"rejected":[{"symbol":"ZZZ-USDT-SWAP","reason":"近期大额代币解锁"}],"summary":"本次扫描共筛选出 N 个推荐币种"}\n\n'
            "retry: 如果 coin_scanner 工具调用失败或返回空，重试最多 3 次（间隔 5 秒）。如果 3 次都失败，推送 alert: send_feishu_text('[选币失败] 原因: {error}')"
        ),
        "schedule": "0 0 * * *",
        "config": {"skill": "coin-scanner", "push": "feishu", "tool": "coin_scanner"},
    },
    {
        "id": "daily-report",
        "prompt": (
            "生成 TSMOM 交易日报。\n"
            "1. 调用 src.push.report_generator.generate_daily_report() 生成日报\n"
            "2. 日报包含：持仓状态、今日交易、今日盈亏、Hurst 值、下次选币时间\n"
            "3. 通过 send_feishu_card 推送到飞书"
        ),
        "schedule": "0 14 * * *",
        "config": {"report": "daily", "push": "feishu"},
    },
    {
        "id": "weekly-report",
        "prompt": (
            "生成 TSMOM 交易周报。\n"
            "1. 调用 src.push.report_generator.generate_weekly_report() 生成周报数据\n"
            "2. AI 基于数据写周度评估分析（策略表现、建议、风险提示）\n"
            "3. 周报包含：本周统计、胜率、盈亏比、f_kelly 更新、Hurst 值、AI 周度评估\n"
            "4. 通过 send_feishu_card 推送到飞书\n"
            "retry: 如果 generate_weekly_report 失败，重试最多 2 次（间隔 10 秒）。2 次都失败则推送 send_feishu_text('[周报生成失败]')"
        ),
        "schedule": "0 2 * * 1",
        "config": {"report": "weekly", "push": "feishu"},
    },
    {
        "id": "monthly-report",
        "prompt": (
            "生成 TSMOM 交易月报。\n"
            "1. 调用 src.push.report_generator.generate_monthly_report() 生成月报数据\n"
            "2. AI 基于数据写月度评估分析\n"
            "3. 月报包含：本月统计、按币种统计、f_kelly 更新、建议仓位、AI 月度评估\n"
            "4. 通过 send_feishu_card 推送到飞书\n"
            "retry: 如果 generate_monthly_report 失败，重试最多 2 次（间隔 10 秒）。2 次都失败则推送 send_feishu_text('[月报生成失败]')"
        ),
        "schedule": "0 2 1 * *",
        "config": {"report": "monthly", "push": "feishu"},
    },
]


def setup_jobs() -> list[str]:
    """创建所有定时任务，返回已创建的 job ID 列表。"""
    store = ScheduledResearchJobStore()
    now_ms = int(time.time() * 1000)
    created: list[str] = []

    for job_def in JOBS:
        job_id = job_def["id"]
        existing = store.get(job_id)
        if existing is not None:
            logger.info("job %s already exists, updating...", job_id)
            existing.prompt = job_def["prompt"]
            existing.schedule = job_def["schedule"]
            existing.config = job_def["config"]
            existing.status = JobStatus.PENDING
            existing.next_run_at = now_ms
            store.upsert(existing)
        else:
            job = ScheduledResearchJob(
                id=job_id,
                prompt=job_def["prompt"],
                schedule=job_def["schedule"],
                next_run_at=now_ms,
                config=job_def["config"],
            )
            store.upsert(job)
            logger.info("created job: %s", job_id)
        created.append(job_id)

    logger.info("setup complete: %d jobs", len(created))
    return created


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_jobs()
    print("定时任务创建完成")
    for j in JOBS:
        print(f"  {j['id']}: {j['schedule']}")