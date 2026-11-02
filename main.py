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
SOURCE_LIST_FILE = "123.txt"
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


def read_urls_from_file(file_path=SOURCE_LIST_FILE):
    urls = []
    if not os.path.exists(file_path):
        return urls

    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            for line in fp:
                candidate = line.strip()
                if not candidate or candidate.startswith("#"):
                    continue
                if candidate.startswith("http://") or candidate.startswith("https://"):
                    urls.append(candidate)
    except Exception:
        return []

    return urls


def deduplicate_urls(urls):
    seen = set()
    ordered = []
    for url in urls:
        norm = urllib.parse.urlsplit(url).geturl().strip()
        if not norm:
            continue
        key = norm.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(norm)
    return ordered


def fetch_url_content(url, timeout=25):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response.text


def gather_sources():
    urls = []
    urls.extend(read_urls_from_file())
    urls.extend(DEFAULT_PASTE_TO_SOURCES)
    urls = deduplicate_urls(urls)
    print(f"Loaded {len(urls)} sources from 123.txt + defaults")
    return urls


def collect_nodes_from_sources(urls):
    nodes = []
    stats = []

    def worker(url):
        try:
            text = fetch_url_content(url)
            parsed = parse_node_list(text)
            if not parsed:
                return {"url": url, "status": "empty", "count": 0}

            filtered = filter_china_nodes(parsed)
            if not filtered:
                return {"url": url, "status": "filtered", "count": 0}
            return {"url": url, "status": "ok", "count": len(filtered), "nodes": filtered}
        except Exception:
            return {"url": url, "status": "error", "count": 0}

    max_workers = min(MAX_THREAD_WORKERS, max(1, len(urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for result in pool.map(worker, urls):
            stats.append({
                "url": result["url"],
                "status": result["status"],
                "count": result["count"],
            })
            if result.get("nodes"):
                nodes.extend(result["nodes"])

    seen = set()
    unique_nodes = []
    for node in nodes:
        try:
            signature = json.dumps(node, ensure_ascii=False, sort_keys=True)
        except Exception:
            signature = str(node)
        if signature in seen:
            continue
        seen.add(signature)
        unique_nodes.append(node)

    return unique_nodes, stats


def write_subscription_files(nodes):
    os.makedirs("sub", exist_ok=True)

    if not nodes:
        empty_text = "# no valid proxies\n"
        with open(os.path.join("sub", "clash.yml"), "w", encoding="utf-8") as fp:
            fp.write(empty_text)
        with open(os.path.join("sub", "free-nodes.txt"), "w", encoding="utf-8") as fp:
            fp.write(empty_text)
        return

    safe_nodes = filter_china_nodes(nodes)
    safe_nodes = safe_nodes[:MAX_PROXY_OUTPUT]

    try:
        clash_text = yaml.safe_dump({"proxies": safe_nodes}, allow_unicode=True, sort_keys=False)
    except Exception:
        clash_text = json.dumps({"proxies": safe_nodes}, ensure_ascii=False, indent=2)

    plain_lines = []
    for node in safe_nodes:
        if isinstance(node, str):
            plain_lines.append(node)
        elif isinstance(node, dict):
            if "server" in node and "password" in node:
                plain_lines.append(json.dumps(node, ensure_ascii=False, sort_keys=True))
            else:
                plain_lines.append(str(node))
        else:
            plain_lines.append(str(node))

    with open(os.path.join("sub", "clash.yml"), "w", encoding="utf-8") as fp:
        fp.write(clash_text)

    with open(os.path.join("sub", "free-nodes.txt"), "w", encoding="utf-8") as fp:
        fp.write("\n".join(plain_lines))


def save_stats(stats):
    payload = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_sources": len(stats),
        "success_count": sum(1 for item in stats if item.get("status") == "ok"),
        "empty_count": sum(1 for item in stats if item.get("status") == "empty"),
        "error_count": sum(1 for item in stats if item.get("status") == "error"),
        "filtered_count": sum(1 for item in stats if item.get("status") == "filtered"),
        "items": stats,
    }
    with open(STATS_FILE, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)


def main():
    urls = gather_sources()
    print(f"123.txt exists: {os.path.exists(SOURCE_LIST_FILE)}")
    if os.path.exists(SOURCE_LIST_FILE):
        print(f"123.txt sample: {read_urls_from_file()[:3]}")
    nodes, stats = collect_nodes_from_sources(urls)
    print(f"Collected {len(nodes)} valid nodes from {len(stats)} source checks")
    write_subscription_files(nodes)
    save_stats(stats)


if __name__ == "__main__":
    main()


# IMPORTANT: when a source node list is parsed downstream, it should pass through
# filter_china_nodes(...) or cleanse_and_filter_node_text(...) before final output.
# This enforces removal of nodes tagged as China/CN/中国/🇨🇳 in name, address, region,
# or any other node metadata before writing the subscription files.
