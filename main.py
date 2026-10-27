import os
import socket
import time
import concurrent.futures
import base64
import json
import urllib.parse
import re
import requests
import yaml

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

STATS_FILE = "source_stats.json"
MAX_FAIL_COUNT = 3

STRICT_FILTER_MODE = os.environ.get("STRICT_FILTER_MODE", "false").lower() == "true"
MAX_PROXY_OUTPUT = int(os.environ.get("MAX_PROXY_OUTPUT", "3000"))
MAX_LATENCY_MS = int(os.environ.get("MAX_LATENCY_MS", "99999"))
MAX_THREAD_WORKERS = int(os.environ.get("MAX_THREAD_WORKERS", "30"))

DEFAULT_PASTE_TO_SOURCES = [
    "https://paste.to/?14372a45d5d8fbb7#771Xe1zSng91Tx3prCuwVyDeT3hBy9gmsiCBaPDUszb",
    "https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/clash.yml",
    "https://raw.githubusercontent.com/anaer/Sub/main/clash.yaml",
    "https://raw.githubusercontent.com/peasoft/No_FDroid/master/subscribe/clash.yml",
    "https://raw.githubusercontent.com/ripaojiedian/freenode/main/sub/config.yaml",
    "https://raw.githubusercontent.com/mahdibland/V2RayAggregator/master/sub/special/normal.txt",
    "https://raw.githubusercontent.com/freefq/free/master/v2ray.txt",
    "https://raw.githubusercontent.com/ssrsub/ssr/master/ssrsub",
    "https://raw.githubusercontent.com/zhangkaiitugithub/passwall/master/mix_1.yaml",
    "https://raw.githubusercontent.com/ACL4SSR/ACL4SSR/master/Clash/config/ACL4SSR_Online_Mini.ini",
    "https://raw.githubusercontent.com/snakeme/cf-speedtest/master/Clash.yaml",
    "https://raw.githubusercontent.com/Leon406/SubMain/main/sub.yaml",
    "https://www.85la.com/internet-access/free-network-nodes",
    "https://www.85la.com/",
]

PASTE_TO_SPECIAL_ID = "14372a45d5d8fbb7"
PASTE_TO_SPECIAL_HASH = "771Xe1zSng91Tx3prCuwVyDeT3hBy9gmsiCBaPDUszb"

CHINA_NODE_PATTERNS = (
    "cn",
    "china",
    "中国",
    "中國",
    "🇨🇳",
    "china mainland",
    "china telecom",
    "china mobile",
    "chinaunicom",
    "chinanet",
    "zhongguo",
    "大陆",
    "内地",
    "国内",
)


def normalize_filter_text(value):
    if value is None:
        return ""
    text = str(value)
    text = text.lower()
    text = text.replace(" ", "")
    text = text.replace("\u200b", "")
    return text


def has_china_marker(value):
    if value is None:
        return False
    text = normalize_filter_text(value)
    if not text:
        return False

    for marker in CHINA_NODE_PATTERNS:
        if marker in text:
            return True

    if re.search(r"(?<![a-z])cn(?![a-z])", text):
        return True

    if re.search(r"(?:\b|_)(china|cn|zhongguo|guonei|guangdong|beijing|shanghai|hangzhou)(?:\b|_)", text):
        return True

    return False


def gather_node_signals(node):
    signals = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (dict, list, tuple)):
                signals.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            else:
                signals.append(value)

        for key in [
            "name",
            "server",
            "servername",
            "host",
            "sni",
            "address",
            "country",
            "region",
            "province",
            "remark",
            "label",
            "source",
            "url",
            "network",
            "path",
            "cipher",
            "type",
        ]:
            if key in node:
                signals.append(node[key])
    elif isinstance(node, (list, tuple)):
        for item in node:
            signals.append(item)
    elif isinstance(node, str):
        signals.append(node)
    return signals


def should_skip_china_node(node, source_hint=None):
    signals = gather_node_signals(node)
    if source_hint:
        signals.append(source_hint)

    for signal in signals:
        if has_china_marker(signal):
            return True

    return False


def filter_china_nodes(nodes):
    if not nodes:
        return []

    filtered = []
    for node in nodes:
        if not should_skip_china_node(node):
            filtered.append(node)
    return filtered


def parse_node_list(raw_text):
    if raw_text is None:
        return []

    try:
        payload = yaml.safe_load(raw_text)
    except Exception:
        payload = None

    if isinstance(payload, dict):
        for key in ("proxies", "Proxy", "nodes", "list", "proxy"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        if payload:
            return [payload]
        return []

    if isinstance(payload, list):
        return payload

    if isinstance(raw_text, str):
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        return [line for line in lines if line.startswith(("vmess://", "vless://", "ss://", "trojan://", "ssr://", "http://", "https://"))]

    return []


def cleanse_and_filter_node_text(raw_text):
    if not raw_text:
        return raw_text

    parsed_nodes = parse_node_list(raw_text)
    filtered_nodes = filter_china_nodes(parsed_nodes)
    if not filtered_nodes:
        return raw_text

    if isinstance(filtered_nodes[0], str):
        return "\n".join(filtered_nodes)

    try:
        return yaml.safe_dump({"proxies": filtered_nodes}, allow_unicode=True, sort_keys=False)
    except Exception:
        return json.dumps({"proxies": filtered_nodes}, ensure_ascii=False, indent=2)


# IMPORTANT: when a source node list is parsed downstream, it should pass through
# filter_china_nodes(...) or cleanse_and_filter_node_text(...) before final output.
# This enforces removal of nodes tagged as China/CN/中国/🇨🇳 in name, address, region,
# or any other node metadata before writing the subscription files.
