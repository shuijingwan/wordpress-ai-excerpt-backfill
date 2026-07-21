"""Minimal GLM 4.7 client for Chinese WordPress excerpts."""

import os

from src.candidate_execution import SafetyError, validate_generated_excerpt
from src.http_json import HttpJsonError, request_json, urllib_transport


GLM_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
SYSTEM_PROMPT = """你正在为一篇中文个人技术博客文章编写 WordPress 摘要。

请根据文章标题和正文，生成一段可以直接保存到 WordPress post_excerpt 的中文摘要。

要求：
1. 使用一段完整、自然、克制、可信的中文。
2. 概括文章解决的问题、主要操作过程和最终结论。
3. 不添加原文不存在的事实、效果或判断。
4. 不使用营销腔、夸张表达或第一人称之外的虚构经历。
5. 不输出标题、编号、列表、Markdown、HTML、Gutenberg 标记或代码块。
6. 不复制长命令、代码、URL、域名或文件路径。
7. 产品名、插件名和技术名词保持准确。
8. 目标长度为 160～240 个中文字符。
9. 只返回摘要正文，不要解释。"""


class Glm47ExcerptClient:
    def __init__(self, api_key=None, transport=urllib_transport, timeout=90):
        self.api_key = api_key if api_key is not None else os.environ.get("ZHIPU_API_KEY")
        if not self.api_key:
            raise SafetyError("ZHIPU_API_KEY is required")
        self.transport = transport
        self.timeout = timeout

    @staticmethod
    def payload(title, cleaned_content):
        return {
            "model": "glm-4.7",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"文章标题：\n{title}\n\n文章正文：\n{cleaned_content}"},
            ],
            "thinking": {"type": "disabled"}, "do_sample": False,
            "stream": False, "max_tokens": 512,
        }

    def generate(self, title, cleaned_content):
        response = request_json(
            self.transport, "POST", GLM_URL,
            {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            self.payload(title, cleaned_content), self.timeout,
        )
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise HttpJsonError("GLM response lacks choices[0].message.content") from error
        return validate_generated_excerpt(content)
