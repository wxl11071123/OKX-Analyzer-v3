"""AI 选币评估独立测试脚本。

用法：在服务器上运行:
    python scripts/test_ai_screening.py

仅测试 AI 评估流程（选币 + AgentLoop 调查 + 飞书推送），
不启动交易引擎，不开仓。
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.tools.coin_scanner_tool import CoinScannerTool
from src.providers.chat import ChatLLM
from src.agent.loop import AgentLoop
from src.tools import build_registry
from src.push.feishu_sender import send_feishu_card, send_feishu_text


def main():
    logger.info("=== AI 选币评估测试开始 ===")

    # 步骤 1: 程序化选前十
    logger.info("步骤 1: 调用 coin_scanner(top_n=10) ...")
    cs = CoinScannerTool()
    raw = cs.execute(top_n=10)
    data = json.loads(raw)
    candidates = data.get("candidates", [])
    if not candidates:
        logger.warning("无候选币种，终止")
        send_feishu_text("测试: coin_scanner 无候选，无法继续")
        return

    logger.info("程序化初筛 %d 个候选: %s", len(candidates), [c["symbol"] for c in candidates])

    # 构建候选文本
    candidates_text = ""
    for i, c in enumerate(candidates, 1):
        quality_map = {"green": "🟢强", "yellow": "🟡标准", "blue": "🔵弱"}
        q = quality_map.get(c.get("signal_quality", ""), "")
        warn = f" ⚠️{c['funding_warn']}" if c.get("funding_warn") else ""
        candidates_text += (
            f"{i}. {c['symbol']} | {c['direction']} | 现价{c['last_price']:.4f} | "
            f"TSMOM {c['tsmom_pct']:+.1f}% | Hurst {c['hurst']:.3f} | "
            f"ADX {c['adx']:.1f} | 信号 {q} | "
            f"费率{c.get('funding_rate', 0):.4f}%{warn}\n"
        )

    prompt = (
        "你是 TSMOM 自动交易系统的选币审查 AI。这是一次测试运行。\n\n"
        "=== 程序化初筛结果（前十名，已按信号强度排序） ===\n"
        f"{candidates_text}\n"
        "=== 你的调查评估任务 ===\n"
        "你需要对上述每个候选币种进行调查，评估其非技术面风险，然后决定批准或拒绝。\n\n"
        "调查步骤（逐个币种执行）：\n"
        "1. 用 web_search 搜索「[币名] token unlock 2026」查看近期是否有代币解锁\n"
        "2. 用 crypto_news 搜索该币名的关键词，查看是否有负面新闻\n"
        "3. 用 web_search 搜索「[币名] hack exploit 2026」查看是否有安全事件\n"
        "4. 用 web_search 搜索「[币名] delist delisting 2026」查看是否有退市风险\n\n"
        "高效原则：\n"
        "- 不用每个币都搜全部4步，如果前两步没发现问题，就可以通过\n"
        "- 排前面的强信号币优先调查\n"
        "- 不要因不确定而拒绝——只拒绝确认存在风险的币种\n\n"
        "审批原则：\n"
        "- 没有明显负面信息的币种默认通过（放入 approved）\n"
        "- 存在已知代币解锁/黑客/退市/治理风险的放入 rejected，写清楚理由\n"
        "- 信号质量 green > yellow > blue，强信号优先通过\n\n"
        "最终输出——你必须输出严格的 JSON（不要加 markdown 代码块标记）：\n"
        '{"approved":["XXX-USDT-SWAP","YYY-USDT-SWAP"],'
        '"rejected":["ZZZ-USDT-SWAP"],'
        '"reasons":{"ZZZ-USDT-SWAP":"具体拒绝理由"},'
        '"summary":"总结：批准N个/拒绝M个，简要说明主要风险"}'
    )

    # 步骤 2: AI 评估
    logger.info("步骤 2: 启动 AI AgentLoop 评估...")
    send_feishu_text("🧪 [测试] AI 选币评估开始，预计 1-3 分钟...")

    try:
        llm = ChatLLM()
        registry = build_registry()
        agent = AgentLoop(
            registry=registry,
            llm=llm,
            max_iterations=40,
        )
        result = agent.run(
            user_message=prompt,
            session_id="test_ai_screening",
        )
        content = (result.get("content") or "").strip()
        logger.info("AI 回复长度: %d 字符", len(content))
        logger.debug("AI 原始回复: %s", content[:1000])

        if not content:
            logger.error("AI 返回空内容")
            send_feishu_text("测试失败: AI 返回空内容")
            return

        # 尝试从 markdown 代码块中提取 JSON
        json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
        if json_match:
            content = json_match.group(1).strip()

        # 尝试从内容中提取第一个完整的 JSON 对象
        if not content.startswith("{"):
            obj_start = content.find("{")
            if obj_start >= 0:
                content = content[obj_start:]

        ai_result = json.loads(content)
        approved = ai_result.get("approved", [])
        rejected = ai_result.get("rejected", [])
        reasons = ai_result.get("reasons", {})
        summary = ai_result.get("summary", "")

        logger.info("AI 评估结果: 批准 %d, 拒绝 %d", len(approved), len(rejected))
        logger.info("批准: %s", approved)
        logger.info("拒绝: %s", rejected)
        logger.info("理由: %s", reasons)
        logger.info("摘要: %s", summary)

        # 步骤 3: 飞书推送
        symbols_info = {c["symbol"]: c for c in candidates}

        lines = ["**🧪 [测试] AI 选币评估**\n\n"]

        if approved:
            lines.append(f"**✅ 批准交易 ({len(approved)})**\n")
            for sym in approved:
                info = symbols_info.get(sym, {})
                d = "📈" if info.get("direction") == "long" else "📉"
                q = info.get("signal_quality", "")
                q_emoji = {"green": "🟢", "yellow": "🟡", "blue": "🔵"}.get(q, "")
                lines.append(
                    f"{d} {sym} {info.get('direction', '?')} "
                    f"TSMOM {info.get('tsmom_pct', 0):+.1f}% {q_emoji}\n"
                )
            lines.append("")

        if rejected:
            lines.append(f"**❌ 拒绝交易 ({len(rejected)})**\n")
            for sym in rejected:
                reason = reasons.get(sym, "未提供理由")
                lines.append(f"• {sym}: {reason}\n")
            lines.append("")

        if summary:
            lines.append(f"**📋 评估摘要**\n{summary}\n")

        send_feishu_card("[测试] AI 选币评估", "".join(lines))
        logger.info("=== AI 选币评估测试完成 ===")

    except json.JSONDecodeError:
        logger.exception("JSON 解析失败，原始: %s", content[:500] if "content" in dir() else "N/A")
        send_feishu_text("测试失败: AI 返回 JSON 无法解析")
    except Exception:
        logger.exception("AI 评估异常")
        send_feishu_text(f"测试失败: {sys.exc_info()[1]}")


if __name__ == "__main__":
    main()
