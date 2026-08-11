#!/usr/bin/env python3
"""
XHS Reader - Streamable HTTP 版
小红书图文解析 + 腾讯混元 Vision 识图
原项目: github.com/raineiris/operit-xhs-reader-mcp
改造: stdio -> streamable-http, 适配 Railway 等云平台 ($PORT)
"""

import json
import re
import base64
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from mcp.server.fastmcp import FastMCP

_port = int(os.environ.get("PORT", 8000))
mcp = FastMCP("Xiaohongshu_Reader_HTTP", host="0.0.0.0", port=_port, json_response=True, stateless_http=True)

HUNYUAN_BASE_URL = "https://api.hunyuan.cloud.tencent.com/v1"

SYSTEM_PROMPT = """你是一个图片内容提取助手。你的任务是根据图片类型，用最合适的格式输出内容。

核心规则：
1. 对话截图（聊天气泡界面）：你必须标注每条消息的发送方。右侧气泡=截图者本人，左侧气泡=对方。格式为"右(昵称)：内容"和"左(昵称)：内容"，每条消息一行，从上到下排列。如果能看到昵称就用昵称，看不到就只写"左""右"。
2. 纯文字/文档图片：直接提取文字，保持原文格式。
3. 其他图片：一句话描述画面。

绝对不要添加分析、总结或解释。"""


def get_api_key():
    """优先读环境变量，其次读 config.env 配置文件"""
    key = os.environ.get("HUNYUAN_API_KEY", "")
    if not key:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env"), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("HUNYUAN_API_KEY=") and len(line) > len("HUNYUAN_API_KEY="):
                        key = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return key


def describe_image(image_url: str) -> str:
    """下载图片，调混元vision，返回图片内容描述"""
    api_key = get_api_key()
    if not api_key:
        return "[未配置HUNYUAN_API_KEY]"
    try:
        # 带 Referer 下载，提高小红书 CDN 图片的可用性
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/",
        }
        resp = requests.get(image_url, headers=headers, timeout=20)
        resp.raise_for_status()
        img_b64 = base64.b64encode(resp.content).decode("utf-8")
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "webp" in content_type:
            media_type = "image/webp"
        elif "png" in content_type:
            media_type = "image/png"
        else:
            media_type = "image/jpeg"

        payload = {
            "model": "hunyuan-vision",
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{img_b64}"}
                        },
                        {
                            "type": "text",
                            "text": "请识别这张图片并按规则输出。如果是聊天截图，必须用'左(昵称)：xxx'和'右(昵称)：xxx'格式逐条标注每条消息的发送方。"
                        }
                    ]
                }
            ],
            "max_tokens": 1500
        }
        r = requests.post(
            f"{HUNYUAN_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[图片理解失败: {str(e)}]"


def describe_images_parallel(image_urls: list) -> dict:
    """并行处理所有图片，返回 {index: description}"""
    results = {}
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_idx = {
            executor.submit(describe_image, url): i
            for i, url in enumerate(image_urls)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = f"[图片处理异常: {str(e)}]"
    return results


def get_xhs_data(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.xiaohongshu.com/",
    }
    try:
        response = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        response.raise_for_status()
        html = response.text
        match = re.search(r'window\.__INITIAL_STATE__=({.+?})</script>', html)
        if not match:
            title_match = re.search(r'<title>(.+?)</title>', html)
            title = title_match.group(1) if title_match else "无标题"
            return {"title": title, "desc": "反爬限制，未能抓取完整内容。", "images": []}
        state_str = match.group(1).replace('undefined', 'null')
        data = json.loads(state_str)
        note_data = data.get("note", {}).get("noteDetailMap", {})
        if not note_data:
            return {"title": "无标题", "desc": "未找到笔记内容", "images": []}
        first_key = list(note_data.keys())[0]
        note = note_data[first_key].get("note", {})
        title = note.get("title", "")
        desc = note.get("desc", "")
        images = []
        for img in note.get("imageList", []):
            url_default = img.get("urlDefault")
            if url_default:
                if not url_default.startswith("http"):
                    url_default = f"https://sns-webpic-qc.xhscdn.com/{url_default}"
                images.append(url_default)
        return {"title": title, "desc": desc, "images": images}
    except Exception as e:
        return {"title": "解析出错", "desc": str(e), "images": []}


@mcp.tool()
def read_xiaohongshu_full(url: str) -> list:
    """
    读取小红书图文帖子。输入帖子链接，返回标题、文案，并用混元vision理解每张图片内容。
    支持 xiaohongshu.com/explore/xxx 链接（含短链 xhslink.com，会自动跟随跳转）。
    """
    data = get_xhs_data(url)
    images = data.get("images", [])

    img_descriptions = ""
    if images:
        results = describe_images_parallel(images)
        for i in range(len(images)):
            desc = results.get(i, "[未获取到描述]")
            img_descriptions += f"\n【第{i+1}张图片内容】\n{desc}\n"

    text_content = (
        f"【小红书帖子内容】\n\n"
        f"标题: {data['title']}\n\n"
        f"文案:\n{data['desc']}\n"
        f"{img_descriptions}"
    )
    return [text_content]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
