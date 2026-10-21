import os
import socket
import time
import concurrent.futures
import base64
import json
import urllib.parse
import requests
import yaml

STATS_FILE = "source_stats.json"
MAX_FAIL_COUNT = 3

# 全量抓取 + 全量推送模式：默认不严格过滤，仅在显式开启 STRICT_FILTER_MODE=true 时启用严格筛选
STRICT_FILTER_MODE = os.environ.get("STRICT_FILTER_MODE", "false").lower() == "true"
MAX_PROXY_OUTPUT = int(os.environ.get("MAX_PROXY_OUTPUT", "300"))
MAX_LATENCY_MS = int(os.environ.get("MAX_LATENCY_MS", "600"))
MAX_THREAD_WORKERS = int(os.environ.get("MAX_THREAD_WORKERS", "30"))


def load_failure_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_failure_stats(stats):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存统计状态异常: {e}")


def update_123_txt_file(removed_urls):
    if not os.path.exists("123.txt") or not removed_urls:
        return
    try:
        with open("123.txt", "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            clean_line = line.strip()
            if clean_line in removed_urls:
                print(f"🗑️ 已自动从 123.txt 中淘汰并清理失效源: {clean_line}")
                continue
            new_lines.append(line)

        with open("123.txt", "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"⚠️ 更新 123.txt 失败: {e}")


def country_code_to_emoji(country_code):
    if not country_code or len(country_code) != 2:
        return "🏳️"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())


def normalize_proxy_name(name, default):
    text = str(name or default).strip()
    return text if text else default


def ensure_safe_proxy(proxy):
    if not isinstance(proxy, dict):
        return None
    server = str(proxy.get("server", "")).strip()
    if not server:
        return None
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError):
        return None
    if not (1 <= port <= 65535):
        return None
    proxy["server"] = server
    proxy["port"] = port
    proxy["type"] = str(proxy.get("type", "unknown")).strip() or "unknown"
    proxy["name"] = normalize_proxy_name(proxy.get("name"), f"{proxy['type'].upper()}-{server}:{port}")
    return proxy


def proxy_signature(proxy):
    proxy = ensure_safe_proxy(proxy)
    if not proxy:
        return None
    return (
        str(proxy.get("type", "")).lower(),
        str(proxy.get("server", "")).lower(),
        int(proxy.get("port", 0)),
        str(proxy.get("uuid", "")).strip(),
        str(proxy.get("password", "")).strip(),
        str(proxy.get("cipher", "")).strip(),
    )


def deduplicate_proxies(proxies):
    seen = set()
    result = []
    for proxy in proxies:
        sig = proxy_signature(proxy)
        if sig is None:
            continue
        if sig in seen:
            continue
        seen.add(sig)
        result.append(ensure_safe_proxy(proxy))
    return result


def batch_get_locations(proxies):
    queries = []
    ip_to_proxy_indices = {}

    for idx, proxy in enumerate(proxies):
        server = proxy.get("server")
        if not server:
            continue
        try:
            ip = socket.gethostbyname(server)
            queries.append({"query": ip, "fields": "status,country,countryCode"})
            ip_to_proxy_indices.setdefault(ip, []).append(idx)
        except Exception:
            continue

    if not queries:
        for proxy in proxies:
            proxy.setdefault("geo_country", "其他")
            proxy.setdefault("geo_flag", "🌍")
        return proxies

    batch_url = "http://ip-api.com/batch?lang=zh-CN"
    try:
        response = requests.post(batch_url, json=queries, timeout=10)
        if response.status_code == 200:
            for res in response.json():
                if res.get("status") == "success":
                    ip = res.get("query")
                    country = res.get("country", "未知")
                    code = res.get("countryCode", "")
                    flag = country_code_to_emoji(code)
                    for idx in ip_to_proxy_indices.get(ip, []):
                        proxies[idx]["geo_country"] = country
                        proxies[idx]["geo_flag"] = flag
    except Exception as e:
        print(f"⚠️ 批量查询地理位置异常: {e}")

    for proxy in proxies:
        proxy.setdefault("geo_country", "其他")
        proxy.setdefault("geo_flag", "🌍")

    return proxies


def test_tcp_latency(server, port, timeout=3):
    start_time = time.time()
    try:
        with socket.create_connection((server, int(port)), timeout=timeout):
            latency = (time.time() - start_time) * 1000
            return round(latency, 2)
    except Exception:
        return None


def proxy_priority(proxy):
    latency = float(proxy.get("latency", 99999))
    country = str(proxy.get("geo_country", "其他")).strip()
    score = latency
    if country in ("其他", "未知"):
        score += 300
    if proxy.get("type") in {"ssr", "unknown"}:
        score += 120
    return score


def strict_filter_proxies(valid_proxies, max_output=None, max_latency_ms=None):
    if max_output is None:
        max_output = MAX_PROXY_OUTPUT
    if max_latency_ms is None:
        max_latency_ms = MAX_LATENCY_MS

    strict = []
    for proxy in valid_proxies:
        latency = float(proxy.get("latency", 99999))
        country = str(proxy.get("geo_country", "其他")).strip()
        if latency > max_latency_ms:
            continue
        if country in ("其他", "未知"):
            continue
        strict.append(proxy)

    if not strict:
        strict = [p for p in valid_proxies if float(p.get("latency", 99999)) <= max_latency_ms]

    strict.sort(key=lambda p: (proxy_priority(p), p.get("latency", 99999)))

    deduped = []
    seen = set()
    for proxy in strict:
        key = (str(proxy.get("server", "")).lower(), int(proxy.get("port", 0)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(proxy)

    return deduped[:max_output]


def select_final_proxies(valid_proxies):
    if STRICT_FILTER_MODE:
        return strict_filter_proxies(valid_proxies, MAX_PROXY_OUTPUT, MAX_LATENCY_MS)

    deduped = []
    seen = set()
    for proxy in sorted(valid_proxies, key=lambda p: (p.get("latency") is None, p.get("latency") or 999999, p.get("geo_country") == "其他")):
        key = (str(proxy.get("server", "")).lower(), int(proxy.get("port", 0)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(proxy)
    return deduped[:MAX_PROXY_OUTPUT]


# ==================== 万能订阅内容解析器 ====================
def parse_vmess_link(link):
    try:
        content = link[8:]
        padding = 4 - (len(content) % 4)
        if padding < 4:
            content += "=" * padding
        decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
        data = json.loads(decoded)
        return {
            "name": data.get("ps", "Vmess-Node"),
            "type": "vmess",
            "server": data.get("add"),
            "port": int(data.get("port", 443)),
            "uuid": data.get("id"),
            "alterId": int(data.get("aid", 0)),
            "cipher": "auto",
            "network": data.get("net", "tcp"),
            "tls": True if data.get("tls") == "tls" else False,
            "ws-opts": {"path": data.get("path", "/"), "headers": {"Host": data.get("host", "")}} if data.get("net") == "ws" else None,
        }
    except Exception:
        return None


def parse_ss_link(link):
    try:
        part = link[5:]
        if "#" in part:
            part, name = part.split("#", 1)
            name = urllib.parse.unquote(name)
        else:
            name = "SS-Node"
        if "@" in part:
            user_info_b64, server_port = part.rsplit("@", 1)
            padding = 4 - (len(user_info_b64) % 4)
            if padding < 4:
                user_info_b64 += "=" * padding
            try:
                user_info = base64.b64decode(user_info_b64).decode("utf-8", errors="ignore")
            except Exception:
                user_info = user_info_b64
            if ":" in user_info:
                cipher, password = user_info.split(":", 1)
            else:
                return None
            if ":" in server_port:
                server, port = server_port.rsplit(":", 1)
                return {
                    "name": name,
                    "type": "ss",
                    "server": server,
                    "port": int(port),
                    "cipher": cipher,
                    "password": password,
                }
    except Exception:
        return None
    return None


def parse_trojan_link(link):
    try:
        part = link[9:]
        if "#" in part:
            part, name = part.split("#", 1)
            name = urllib.parse.unquote(name)
        else:
            name = "Trojan-Node"
        if "@" in part:
            password, server_port = part.split("@", 1)
            if "?" in server_port:
                server_port, query = server_port.split("?", 1)
            else:
                query = ""
            if ":" in server_port:
                server, port = server_port.split(":", 1)
                sni = server
                if query:
                    parsed_q = urllib.parse.parse_qs(query)
                    if "sni" in parsed_q:
                        sni = parsed_q["sni"][0]
                return {
                    "name": name,
                    "type": "trojan",
                    "server": server,
                    "port": int(port),
                    "password": password,
                    "sni": sni,
                    "skip-cert-verify": True,
                }
    except Exception:
        return None
    return None


def parse_ssr_link(link):
    try:
        part = link[6:]
        if "#" in part:
            part, name = part.split("#", 1)
            name = urllib.parse.unquote(name)
        else:
            name = "SSR-Node"
        decoded = base64.b64decode(part + "=" * (-len(part) % 4)).decode("utf-8", errors="ignore")
        if ":" not in decoded:
            return None
        _proto, remaining = decoded.split(":", 1)
        if "/" in remaining:
            remaining = remaining.split("/", 1)[0]
        if ":" in remaining:
            server, port = remaining.rsplit(":", 1)
            return {
                "name": name,
                "type": "ssr",
                "server": server,
                "port": int(port),
                "cipher": _proto,
                "password": "",
            }
    except Exception:
        return None
    return None


def extract_proxies_from_content(text):
    proxies = []
    text_stripped = (text or "").strip()

    try:
        data = yaml.safe_load(text_stripped)
        if data and isinstance(data, dict) and "proxies" in data and isinstance(data["proxies"], list):
            proxies.extend(data["proxies"])
    except Exception:
        pass

    raw_text = text_stripped
    try:
        if not text_stripped.startswith("http") and len(text_stripped) > 20:
            padding = 4 - (len(text_stripped) % 4)
            if padding < 4:
                text_padded = text_stripped + "=" * padding
            else:
                text_padded = text_stripped
            decoded_bytes = base64.b64decode(text_padded)
            raw_text = decoded_bytes.decode("utf-8", errors="ignore")
    except Exception:
        pass

    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxy = None
        if line.startswith("vmess://"):
            proxy = parse_vmess_link(line)
        elif line.startswith("ss://"):
            proxy = parse_ss_link(line)
        elif line.startswith("trojan://"):
            proxy = parse_trojan_link(line)
        elif line.startswith("ssr://"):
            proxy = parse_ssr_link(line)
        if proxy and proxy.get("server") and proxy.get("port"):
            proxies.append(proxy)

    return deduplicate_proxies(proxies)


# ==========================================================
def generate_advanced_clash_config(valid_proxies, country_groups):
    country_group_names = list(country_groups.keys())
    all_proxy_names = []
    seen_names = set()
    for p in valid_proxies:
        name = p.get("name")
        if name not in seen_names:
            all_proxy_names.append(name)
            seen_names.add(name)

    proxy_groups = [
        {
            "name": "🚀 节点选择",
            "type": "select",
            "proxies": ["♻️ 自动选择"] + country_group_names + ["🎯 全球直连", "🌍 国外媒体"],
        },
        {
            "name": "♻️ 自动选择",
            "type": "url-test",
            "proxies": all_proxy_names,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        },
    ]

    for group_name, proxies in country_groups.items():
        proxy_groups.append({
            "name": group_name,
            "type": "url-test",
            "proxies": proxies,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        })

    streaming_groups = [
        {"name": "🎬 奈飞视频", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "📺 YouTube", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "✈️ 电报消息", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "🌍 国外媒体", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "🍎 苹果服务", "type": "select", "proxies": ["🎯 全球直连", "♻️ 自动选择"]},
        {"name": "🎯 全球直连", "type": "select", "proxies": ["DIRECT"]},
    ]
    proxy_groups.extend(streaming_groups)

    rules = [
        "DOMAIN-SUFFIX,netflix.com,🎬 奈飞视频",
        "DOMAIN-KEYWORD,netflix,🎬 奈飞视频",
        "DOMAIN-SUFFIX,youtube.com,📺 YouTube",
        "DOMAIN-KEYWORD,youtube,📺 YouTube",
        "DOMAIN-SUFFIX,t.me,✈️ 电报消息",
        "DOMAIN-SUFFIX,telegram.org,✈️ 电报消息",
        "IP-CIDR,91.108.4.0/22,✈️ 电报消息,no-resolve",
        "DOMAIN-SUFFIX,apple.com,🍎 苹果服务",
        "DOMAIN-SUFFIX,icloud.com,🍎 苹果服务",
        "DOMAIN-SUFFIX,google.com,🌍 国外媒体",
        "DOMAIN-SUFFIX,github.com,🌍 国外媒体",
        "GEOIP,CN,🎯 全球直连",
        "MATCH,🚀 节点选择",
    ]

    return {
        "proxies": valid_proxies,
        "proxy-groups": proxy_groups,
        "rules": rules,
    }


def vmess_to_link(p):
    config = {
        "v": "2",
        "ps": p.get("name", "Vmess"),
        "add": p.get("server"),
        "port": str(p.get("port")),
        "id": p.get("uuid"),
        "aid": str(p.get("alterId", 0)),
        "net": p.get("network", "tcp"),
        "type": "none",
        "host": p.get("ws-opts", {}).get("headers", {}).get("Host", "") if p.get("network") == "ws" else "",
        "path": p.get("ws-opts", {}).get("path", "") if p.get("network") == "ws" else "",
        "tls": "tls" if p.get("tls") else "",
    }
    return f"vmess://{base64.b64encode(json.dumps(config, ensure_ascii=False).encode('utf-8')).decode('utf-8')}"


def ss_to_link(p):
    user_info = f"{p.get('cipher')}:{p.get('password')}"
    b64_user = base64.b64encode(user_info.encode("utf-8")).decode("utf-8").strip("=")
    return f"ss://{b64_user}@{p.get('server')}:{p.get('port')}#{urllib.parse.quote(p.get('name', 'SS'))}"


def trojan_to_link(p):
    sni = p.get("sni", "")
    params = f"?sni={sni}&allowInsecure=1" if sni else "?allowInsecure=1"
    return f"trojan://{p.get('password')}@{p.get('server')}:{p.get('port')}{params}#{urllib.parse.quote(p.get('name', 'Trojan'))}"


def generate_v2ray_subscription(valid_proxies, output_path="sub/v2ray.txt"):
    links = []
    for p in valid_proxies:
        ptype = p.get("type")
        try:
            if ptype == "vmess":
                links.append(vmess_to_link(p))
            elif ptype == "ss":
                links.append(ss_to_link(p))
            elif ptype == "trojan":
                links.append(trojan_to_link(p))
        except Exception:
            pass
    if links:
        raw = "\n".join(links)
        b64 = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(b64)


def send_notification(send_key, total_count, final_count):
    if not send_key:
        print("ℹ️ 未检测到 SEND_KEY，跳过微信推送。")
        return

    repo = os.environ.get("GITHUB_REPOSITORY", "username/repo")
    parts = repo.split("/")
    pages_url = f"https://{parts[0]}.github.io/{parts[1] if len(parts) > 1 else 'repo'}"
    clash_url = f"{pages_url}/sub/clash.yaml"
    v2ray_url = f"{pages_url}/sub/v2ray.txt"

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    desp = (
        "### 🟢 免费节点自动更新报告（全量抓取 + 全量推送版）\n"
        f"- **抓取到的节点**: `{total_count}` 个\n"
        f"- **实际推送的节点**: `{final_count}` 个\n"
        "- **模式**: 全量抓取，不做严格过滤\n\n"
        "---\n\n"
        "#### 📌 Clash 订阅直链\n"
        "```text\n"
        f"{clash_url}\n"
        "```\n\n"
        "#### 📌 V2Ray 订阅直链\n"
        "```text\n"
        f"{v2ray_url}\n"
        "```"
    )

    params = {
        "title": f"Free Nodes All Push: {final_count} Nodes",
        "desp": desp,
    }

    try:
        res = requests.post(url, params=params, timeout=10)
        print("📨 Server酱推送结果:", res.text)
    except Exception as e:
        print(f"⚠️ 推送通知失败: {e}")


def main():
    try:
        failure_stats = load_failure_stats()
        removed_sources = []

        source_urls = [
            "https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/clash.yml",
            "https://raw.githubusercontent.com/anaer/Sub/main/clash.yaml",
        ]

        if os.path.exists("123.txt"):
            try:
                with open("123.txt", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and line not in source_urls:
                            source_urls.append(line)
                print(f"📄 已成功加载 123.txt 外部源，待检查总源数: {len(source_urls)}")
            except Exception as e:
                print(f"⚠️ 读取 123.txt 异常: {e}")

        source_urls = list(dict.fromkeys(source_urls))
        merged_proxies = []
        source_health_report = []

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        })

        for url in source_urls:
            print(f"\n📥 正在检查并抓取源: {url}")
            start_t = time.time()
            source_info = {"url": url, "status": "未知", "nodes": 0, "latency": 0}
            is_success = False

            try:
                res = session.get(url, timeout=15)
                source_info["latency"] = round((time.time() - start_t) * 1000, 2)

                if res.status_code == 200:
                    extracted = extract_proxies_from_content(res.text)
                    if extracted:
                        node_count = len(extracted)
                        merged_proxies.extend(extracted)
                        source_info["status"] = "🟢 正常可用"
                        source_info["nodes"] = node_count
                        is_success = True
                        print(f"✅ [健康] 解析成功 | 耗时: {source_info['latency']}ms | 有效节点: {node_count}个")
                    else:
                        source_info["status"] = "🟡 解析无有效节点"
                        print("⚠️ [警告] 链接可达但未解析出有效节点")
                else:
                    source_info["status"] = f"🔴 HTTP失效({res.status_code})"
                    print(f"❌ [失效] 状态码异常: {res.status_code}")

            except Exception as e:
                source_info["status"] = "⏱️ 连接超时或解析失败"
                print(f"⏱️ [错误] 抓取异常: {e}")

            current_fails = failure_stats.get(url, 0)
            if is_success:
                failure_stats[url] = 0
            else:
                current_fails += 1
                failure_stats[url] = current_fails
                print(f"⚠️ [警告] 该源连续失败次数: {current_fails}/{MAX_FAIL_COUNT}")
                if current_fails >= MAX_FAIL_COUNT:
                    print(f"🚨 [淘汰] 订阅源 {url} 连续失败超过 {MAX_FAIL_COUNT} 次，将自动剔除！")
                    removed_sources.append(url)
                    failure_stats.pop(url, None)

            source_health_report.append(source_info)

        save_failure_stats(failure_stats)
        if removed_sources:
            update_123_txt_file(removed_sources)

        print("\n" + "=" * 40)
        print("📊 订阅源健康检查汇总报告")
        print("=" * 40)
        for item in source_health_report:
            print(f"• 🔗 链接: {item['url']}")
            print(f"  状态: {item['status']} | 耗时: {item['latency']}ms | 节点数: {item['nodes']}")
        print("=" * 40 + "\n")

        merged_proxies = deduplicate_proxies(merged_proxies)
        if not merged_proxies:
            print("❌ 未能获取到任何有效原始节点。")
            return

        print(f"🌐 聚合后共有原始节点 {len(merged_proxies)} 个，开始批量获取地理位置...")
        proxies_with_geo = batch_get_locations(merged_proxies)

        print("⚡ 开始并发 TCP 测速，但不再把测速结果当成最终筛选条件...")

        def check_node(proxy):
            server = proxy.get("server")
            port = proxy.get("port")
            if not server or not port:
                return None
            latency = test_tcp_latency(server, port)
            if latency is not None:
                proxy["latency"] = latency
                flag = proxy.get("geo_flag", "🌍")
                country = proxy.get("geo_country", "未知")
                proxy["name"] = f"{flag} [{country}] {proxy.get('name', 'Node')} | {latency}ms"
            else:
                proxy["latency"] = None
                flag = proxy.get("geo_flag", "🌍")
                country = proxy.get("geo_country", "未知")
                proxy["name"] = f"{flag} [{country}] {proxy.get('name', 'Node')} | 未测速"
            return proxy

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREAD_WORKERS) as executor:
            results = list(executor.map(check_node, proxies_with_geo))

        all_proxies = [p for p in results if p is not None]
        all_proxies.sort(key=lambda x: (x.get("latency") is None, x.get("latency") or 999999, x.get("geo_country") == "其他"))
        print(f"🎯 全量抓取完成，当前总节点数: {len(all_proxies)}")

        final_proxies = select_final_proxies(all_proxies)
        print(f"📦 最终输出节点数: {len(final_proxies)}")

        if not final_proxies:
            print("❌ 没有可输出节点，已中止生成订阅。")
            return

        country_groups = {}
        for p in final_proxies:
            group_name = f"{p.get('geo_flag')} {p.get('geo_country')} 节点"
            country_groups.setdefault(group_name, []).append(p["name"])

        clash_config = generate_advanced_clash_config(final_proxies, country_groups)

        for p in clash_config["proxies"]:
            p.pop("latency", None)
            p.pop("geo_country", None)
            p.pop("geo_flag", None)

        os.makedirs("sub", exist_ok=True)
        with open("sub/clash.yaml", "w", encoding="utf-8") as f:
            yaml.dump(clash_config, f, allow_unicode=True, sort_keys=False)
        print("💾 Clash 文件已写入 sub/clash.yaml")

        generate_v2ray_subscription(final_proxies, "sub/v2ray.txt")
        print("💾 V2Ray 文件已写入 sub/v2ray.txt")

        event_name = os.environ.get("GITHUB_EVENT_NAME", "")
        if event_name == "workflow_dispatch":
            send_notification(os.environ.get("SEND_KEY"), len(all_proxies), len(final_proxies))
        else:
            print("ℹ️ 当前为定时自动运行，已跳过 Server酱微信推送。")

        print("🎉 执行完毕！")

    except Exception as e:
        print(f"❌ 运行发生错误: {e}")
        raise


if __name__ == "__main__":
    main()
