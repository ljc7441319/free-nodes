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
