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
ENABLE_TCP_CHECK = os.environ.get("ENABLE_TCP_CHECK", "true").lower() == "true"
TCP_CHECK_TIMEOUT = float(os.environ.get("TCP_CHECK_TIMEOUT", "1.5"))
LIVENESS_CHECK_MAX_NODES = int(os.environ.get("LIVENESS_CHECK_MAX_NODES", "500"))

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

SUPPORTED_CLASH_TYPES = {"vmess", "vless", "trojan", "ss", "ssr"}


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


def encode_base64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def normalize_server(server):
    if server is None:
        return ""
    if isinstance(server, (list, tuple)):
        server = server[0]
    server = str(server).strip().strip("[]")
    if not server:
        return ""
    return server


def is_valid_server_host(server):
    server = normalize_server(server)
    if not server:
        return False
    if server.lower() in {"null", "none", "unknown", "example.com"}:
        return False
    if server.startswith("http://") or server.startswith("https://"):
        return False
    if re.search(r"\s", server):
        return False
    if ":" in server and not re.fullmatch(r"[A-Za-z0-9._:-]+", server):
        return False
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", server):
        return False
    return True


def normalize_node_type(node):
    if not isinstance(node, dict):
        return ""
    node_type = str(node.get("type") or "").lower().strip()
    if node_type:
        return node_type
    if node.get("uuid") and node.get("server"):
        return "vless"
    if node.get("password") and node.get("server") and node.get("cipher"):
        return "ss"
    if node.get("password") and node.get("server"):
        return "trojan"
    if node.get("server") and node.get("port"):
        return "vless"
    return ""


def sanitize_candidate(node):
    if not isinstance(node, dict):
        return None

    node_type = normalize_node_type(node)
    if not node_type:
        return None

    server = normalize_server(node.get("server"))
    if not is_valid_server_host(server):
        return None

    port = safe_int(node.get("port"), None)
    if port is None or port < 1 or port > 65535:
        return None

    sanitized = dict(node)
    sanitized["type"] = node_type
    sanitized["server"] = server
    sanitized["port"] = port

    if node_type in {"vless", "vmess"}:
        uuid = node.get("uuid") or node.get("id")
        if not uuid or not str(uuid).strip():
            return None
        sanitized["uuid"] = str(uuid)
        if node_type == "vmess":
            sanitized["alterId"] = safe_int(node.get("alterId"), 0)
            sanitized["cipher"] = str(node.get("cipher") or "auto")

    if node_type == "trojan":
        password = node.get("password")
        if not password or not str(password).strip():
            return None
        sanitized["password"] = str(password)

    if node_type in {"ss", "ssr"}:
        method = node.get("cipher") or node.get("method")
        password = node.get("password")
        if not method or not password:
            return None
        sanitized["cipher"] = str(method)
        sanitized["password"] = str(password)

    # drop obviously broken entries that carry non-proxy payloads
    if "name" in sanitized:
        name = str(sanitized["name"]).strip()
        if not name or name.lower() in {"none", "null", "unknown"}:
            sanitized["name"] = f"{node_type}-{server}-{port}"

    return sanitized


def check_node_liveness(node, timeout=TCP_CHECK_TIMEOUT):
    if not isinstance(node, dict):
        return False
    server = normalize_server(node.get("server"))
    port = safe_int(node.get("port"), None)
    if not server or port is None:
        return False

    try:
        sock = socket.create_connection((server, port), timeout=timeout)
        sock.close()
        return True
    except Exception:
        return False


def deduplicate_nodes(nodes):
    seen = set()
    out = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        key = json.dumps({
            "type": normalize_node_type(node),
            "server": normalize_server(node.get("server")),
            "port": safe_int(node.get("port"), -1),
            "name": str(node.get("name") or "").strip(),
            "uuid": str(node.get("uuid") or node.get("id") or "").strip(),
            "password": str(node.get("password") or "").strip(),
        }, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(node)
    return out


def filter_usable_nodes(nodes, check_connectivity=ENABLE_TCP_CHECK):
    valid = []
    for node in nodes:
        candidate = sanitize_candidate(node)
        if not candidate:
            continue
        if check_connectivity and not check_node_liveness(candidate):
            continue
        valid.append(candidate)

    return deduplicate_nodes(valid)


def build_vmess_payload(node):
    if not isinstance(node, dict):
        return None
    server = node.get("server")
    port = safe_int(node.get("port"), 0)
    if not server or port <= 0:
        return None
    uuid = node.get("uuid") or node.get("id")
    if not uuid:
        return None

    net = str(node.get("network") or "tcp").lower()
    if net == "httpupgrade":
        net = "ws"
    if net == "xhttp":
        net = "ws"

    path = ""
    if net == "ws":
        ws_opts = node.get("ws-opts") or {}
        if isinstance(ws_opts, dict):
            path = ws_opts.get("path") or ""
    elif net == "grpc":
        grpc_opts = node.get("grpc-opts") or {}
        if isinstance(grpc_opts, dict):
            path = grpc_opts.get("serviceName") or ""
    elif "http-opts" in node and isinstance(node.get("http-opts"), dict):
        http_opts = node.get("http-opts") or {}
        path = http_opts.get("path") or ""
        if isinstance(path, list):
            path = path[0] if path else ""

    host = node.get("servername") or node.get("host") or ""
    tls_flag = bool(node.get("tls"))
    payload = {
        "v": "2",
        "ps": node.get("name") or "proxy",
        "add": server,
        "port": port,
        "id": uuid,
        "aid": safe_int(node.get("alterId"), 0),
        "scy": node.get("cipher") or "auto",
        "net": net,
        "type": "none",
        "host": host,
        "path": path,
        "tls": "tls" if tls_flag else "",
        "sni": node.get("sni") or host or "",
        "alpn": node.get("alpn") or "",
    }
    if net == "ws":
        payload["type"] = "none"
    return payload


def vmess_to_url(node):
    payload = build_vmess_payload(node)
    if not payload:
        return None
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return "vmess://" + encode_base64(data)


def vless_to_url(node):
    if not isinstance(node, dict):
        return None
    server = node.get("server")
    port = node.get("port")
    uuid = node.get("uuid")
    if not server or not port or not uuid:
        return None

    net = str(node.get("network") or "tcp").lower()
    if net == "httpupgrade":
        net = "ws"
    if net == "xhttp":
        net = "ws"

    query = {
        "encryption": "none",
        "security": "tls" if node.get("tls") else "none",
        "type": net if net in {"ws", "grpc", "http", "tcp"} else "tcp",
    }
    if node.get("sni"):
        query["sni"] = str(node.get("sni"))
    if node.get("servername"):
        query["host"] = str(node.get("servername"))
    elif node.get("host"):
        query["host"] = str(node.get("host"))

    if net == "ws":
        ws_opts = node.get("ws-opts") or {}
        if isinstance(ws_opts, dict):
            path = ws_opts.get("path") or ""
            if path:
                query["path"] = path
            headers = ws_opts.get("headers") or {}
            if isinstance(headers, dict):
                host = headers.get("Host") or headers.get("host")
                if host:
                    query["host"] = str(host)
    elif net == "grpc":
        grpc_opts = node.get("grpc-opts") or {}
        if isinstance(grpc_opts, dict):
            service_name = grpc_opts.get("grpc-service-name") or grpc_opts.get("serviceName")
            if service_name:
                query["serviceName"] = str(service_name)

    if node.get("flow"):
        query["flow"] = str(node.get("flow"))
    if node.get("alpn"):
        query["alpn"] = ",".join(str(i) for i in node.get("alpn"))
    if node.get("skip-cert-verify") is True:
        query["verify"] = "false"

    host = query.get("host") or query.get("sni") or server
    uri = f"vless://{uuid}@{server}:{port}?{urllib.parse.urlencode(query, doseq=True)}"
    if node.get("name"):
        return uri + f"#{urllib.parse.quote(str(node.get('name')), safe='')}"
    return uri


def trojan_to_url(node):
    if not isinstance(node, dict):
        return None
    server = node.get("server")
    port = node.get("port")
    password = node.get("password")
    if not server or not port or not password:
        return None

    query = {
        "security": "tls" if node.get("tls") else "none",
    }
    if node.get("sni"):
        query["sni"] = str(node.get("sni"))
    if node.get("servername"):
        query["host"] = str(node.get("servername"))
    if node.get("skip-cert-verify") is True:
        query["allowInsecure"] = "1"
    if node.get("network"):
        query["type"] = str(node.get("network"))
    ws_opts = node.get("ws-opts") or {}
    if isinstance(ws_opts, dict) and ws_opts.get("path"):
        query["path"] = ws_opts.get("path")
    uri = f"trojan://{urllib.parse.quote(password, safe='')}@{server}:{port}?{urllib.parse.urlencode(query, doseq=True)}"
    if node.get("name"):
        return uri + f"#{urllib.parse.quote(str(node.get('name')), safe='')}"
    return uri


def ss_to_url(node):
    if not isinstance(node, dict):
        return None
    server = node.get("server")
    port = node.get("port")
    method = node.get("cipher") or node.get("method")
    password = node.get("password")
    if not server or not port or not method or not password:
        return None

    userinfo = urllib.parse.quote(f"{method}:{password}", safe='')
    uri = f"ss://{userinfo}@{server}:{port}"
    if node.get("name"):
        return uri + f"#{urllib.parse.quote(str(node.get('name')), safe='')}"
    return uri


def standardize_node_to_url(node):
    if isinstance(node, str):
        text = node.strip()
        if not text:
            return None
        if text.startswith(("vmess://", "vless://", "ss://", "trojan://")):
            return text
        if text.startswith("{"):
            try:
                node = json.loads(text)
            except Exception:
                return None
        else:
            return None

    if not isinstance(node, dict):
        return None

    node_type = normalize_node_type(node)
    if node_type == "vmess":
        return vmess_to_url(node)
    if node_type == "vless":
        return vless_to_url(node)
    if node_type == "trojan":
        return trojan_to_url(node)
    if node_type in {"ss", "ssr"}:
        return ss_to_url(node)

    if node.get("server") and node.get("port"):
        if node.get("uuid"):
            return vless_to_url(node)
        if node.get("password") and node.get("cipher"):
            return ss_to_url(node)
        if node.get("password"):
            return trojan_to_url(node)

    return None


def sanitize_clash_proxy(node):
    if not isinstance(node, dict):
        return None

    node_type = normalize_node_type(node)
    if node_type not in SUPPORTED_CLASH_TYPES:
        return None

    name = node.get("name") or f"proxy-{len(str(node))}"
    proxy = {"name": str(name)}

    if node_type in {"vless", "vmess", "trojan"}:
        server = node.get("server")
        port = node.get("port")
        if not server or port is None:
            return None
        proxy["type"] = node_type
        proxy["server"] = str(server)
        proxy["port"] = int(port)
        if node.get("tls") is not None:
            proxy["tls"] = bool(node.get("tls"))
        if node.get("skip-cert-verify") is not None:
            proxy["skip-cert-verify"] = bool(node.get("skip-cert-verify"))
        if node.get("udp") is not None:
            proxy["udp"] = bool(node.get("udp"))
        if node.get("sni"):
            proxy["sni"] = str(node.get("sni"))
        if node.get("servername"):
            proxy["servername"] = str(node.get("servername"))
        if node.get("network"):
            proxy["network"] = str(node.get("network"))
        if node.get("ws-opts"):
            proxy["ws-opts"] = node.get("ws-opts")
        if node.get("http-opts"):
            proxy["http-opts"] = node.get("http-opts")
        if node.get("grpc-opts"):
            proxy["grpc-opts"] = node.get("grpc-opts")
        if node.get("alpn"):
            proxy["alpn"] = node.get("alpn")
        if node.get("client-fingerprint"):
            proxy["client-fingerprint"] = str(node.get("client-fingerprint"))
        if node_type == "vless":
            if node.get("uuid"):
                proxy["uuid"] = str(node.get("uuid"))
            if node.get("flow"):
                proxy["flow"] = str(node.get("flow"))
        elif node_type == "vmess":
            if node.get("uuid"):
                proxy["uuid"] = str(node.get("uuid"))
            if node.get("alterId") is not None:
                proxy["alterId"] = safe_int(node.get("alterId"), 0)
            if node.get("cipher"):
                proxy["cipher"] = str(node.get("cipher"))
        elif node_type == "trojan":
            if node.get("password"):
                proxy["password"] = str(node.get("password"))
    elif node_type in {"ss", "ssr"}:
        server = node.get("server")
        port = node.get("port")
        if not server or port is None:
            return None
        proxy["type"] = "ss"
        proxy["server"] = str(server)
        proxy["port"] = int(port)
        if node.get("cipher"):
            proxy["cipher"] = str(node.get("cipher"))
        if node.get("method"):
            proxy["cipher"] = str(node.get("method"))
        if node.get("password"):
            proxy["password"] = str(node.get("password"))
        if node.get("udp") is not None:
            proxy["udp"] = bool(node.get("udp"))

    if not proxy:
        return None
    return proxy


def generate_clash_config(nodes):
    proxies = []
    for node in nodes:
        sanitized = sanitize_clash_proxy(node)
        if sanitized:
            proxies.append(sanitized)

    proxy_names = [p["name"] for p in proxies]
    return {
        "proxies": proxies,
        "proxy-groups": [{
            "name": "Auto",
            "type": "select",
            "proxies": proxy_names,
        }],
        "rules": ["MATCH,Auto"],
    }


def write_subscription_files(nodes):
    os.makedirs("sub", exist_ok=True)

    usable_nodes = filter_usable_nodes(nodes, check_connectivity=ENABLE_TCP_CHECK)
    safe_nodes = usable_nodes[:MAX_PROXY_OUTPUT]

    standard_links = []
    for node in safe_nodes:
        link = standardize_node_to_url(node)
        if link:
            standard_links.append(link)

    if not standard_links:
        empty_text = "# no valid proxies\n"
        for relative_path in (
            "sub/clash.yml",
            "sub/clash.yaml",
            "sub/free-nodes.txt",
            "sub/v2ray.txt",
        ):
            with open(relative_path, "w", encoding="utf-8") as fp:
                fp.write(empty_text)
        return

    clash_config = generate_clash_config(safe_nodes)
    clash_text = yaml.safe_dump(clash_config, allow_unicode=True, sort_keys=False)

    plain_text = "\n".join(standard_links)
    for relative_path, content in (
        ("sub/clash.yml", clash_text),
        ("sub/clash.yaml", clash_text),
        ("sub/free-nodes.txt", plain_text),
        ("sub/v2ray.txt", plain_text),
    ):
        with open(relative_path, "w", encoding="utf-8") as fp:
            fp.write(content)


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
    filtered_nodes = filter_usable_nodes(nodes, check_connectivity=ENABLE_TCP_CHECK)
    print(f"Collected {len(nodes)} raw nodes, kept {len(filtered_nodes)} usable nodes from {len(stats)} source checks")
    write_subscription_files(filtered_nodes)
    save_stats(stats)


if __name__ == "__main__":
    main()


# IMPORTANT: when a source node list is parsed downstream, it should pass through
# filter_china_nodes(...) or cleanse_and_filter_node_text(...) before final output.
# This enforces removal of nodes tagged as China/CN/中国/🇨🇳 in name, address, region,
# or any other node metadata before writing the subscription files.
# The generator now filters unusable entries by protocol validity and, when enabled,
# a short TCP reachability check; this preserves only nodes likely to work in clients.
