"""新闻翻译——使用 deep-translator (Google Translate) 免费翻译英→中。"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_translator = None


def _get_translator():
    global _translator
    if _translator is None:
        try:
            from deep_translator import GoogleTranslator
            _translator = GoogleTranslator(source="en", target="zh-CN")
        except ImportError:
            logger.warning("deep-translator 未安装，新闻翻译不可用。pip install deep-translator")
            _translator = False
    return _translator if _translator else None


def translate_text(text: str, max_length: int = 500) -> str:
    """翻译文本为中文，失败返回原文。"""
    if not text or not text.strip():
        return text

    translator = _get_translator()
    if not translator:
        return text

    try:
        # 限制长度避免超时
        truncated = text[:max_length]
        result = translator.translate(truncated)
        time.sleep(0.5)  # 避免频率限制
        return result
    except Exception as e:
        logger.debug(f"翻译失败: {e}")
        return text


def translate_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批量翻译新闻标题和摘要。"""
    for article in articles:
        title = article.get("title", "")
        summary = article.get("summary", "")
        if title:
            article["title_zh"] = translate_text(title, max_length=200)
        if summary:
            article["summary_zh"] = translate_text(summary, max_length=300)
    return articles
