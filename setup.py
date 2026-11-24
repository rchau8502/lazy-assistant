#!/usr/bin/env python3
"""小助 · 懒人包配置向导：选好模型服务，生成 .env。

只依赖标准库，可以在装依赖之前直接跑。
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENV_PATH = BASE / ".env"
EXAMPLE_PATH = BASE / ".env.example"

PRESETS = {
    "1": {
        "name": "DeepSeek 官方",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "need_key": True,
        "hint": "在 platform.deepseek.com 创建 API key，按量计费，很便宜",
    },
    "2": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "need_key": True,
        "hint": "在 platform.openai.com 创建 API key",
    },
    "3": {
        "name": "本地 Ollama / LM Studio",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "need_key": False,
        "hint": "先装好 Ollama 并 ollama pull qwen2.5:7b；LM Studio 的地址一般是 http://127.0.0.1:1234/v1",
    },
    "4": {
        "name": "其他 OpenAI 兼容接口",
        "base_url": "",
        "model": "",
        "need_key": False,
        "hint": "任何兼容 /chat/completions 的服务都可以：vLLM、one-api、硅基流动、OpenRouter…",
    },
}


def ask(prompt: str, default: str = "") -> str:
    tip = f"（默认 {default}）" if default else ""
    raw = input(f"{prompt}{tip}: ").strip()
    return raw or default


def main() -> int:
    print("\n  🧭  小助 · 懒人包配置\n" + "  " + "-" * 34)
    print("  选一个模型服务，之后随时可以改 .env：\n")
    for key, p in PRESETS.items():
        print(f"  {key}. {p['name']}")
    print()

    choice = ask("输入序号", "1")
    while choice not in PRESETS:
        choice = ask("没有这个序号，重新输入", "1")
    preset = PRESETS[choice]

    base_url = ask("  接口地址", preset["base_url"])
    model = ask("  模型名", preset["model"])
    if not base_url or not model:
        print("  接口地址和模型名都不能为空。")
        return 1

    api_key = ""
    if preset["need_key"]:
        print(f"  {preset['hint']}")
        api_key = getpass.getpass("  API key（输入时不显示）: ").strip()
        if not api_key:
            print("  没有 key 的话，也可以用本地模型（选 3）。")
            return 1
    else:
        print("  本地/自建服务通常不需要 key，直接回车跳过。")
        api_key = getpass.getpass("  API key（可留空）: ").strip() if sys.stdin.isatty() else ""

    port = ask("  端口", "8010")
    bot_name = ask("  助手名字", "小助")
    print()

    values = {
        "LLM_API_KEY": api_key,
        "LLM_BASE_URL": base_url,
        "LLM_MODEL": model,
        "PORT": port,
        "BOT_NAME": bot_name,
    }

    example = EXAMPLE_PATH.read_text(encoding="utf-8") if EXAMPLE_PATH.exists() else ""
    lines = []
    for line in example.splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in values:
            lines.append(f"{key}={values[key]}")
        else:
            lines.append(line)
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"  已写入 {ENV_PATH}")
    print(f"  模型：{model}  ·  接口：{base_url}")
    print(f"  下一步：启动服务后打开 http://127.0.0.1:{port}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n  已取消，没有改动任何文件。")
        raise SystemExit(1)
