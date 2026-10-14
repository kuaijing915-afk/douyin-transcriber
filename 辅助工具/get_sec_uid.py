from pathlib import Path
"""通过抖音 API 获取当前登录用户的 sec_uid - v3"""
import json
from curl_cffi import requests

cookie = open(Path(__file__).resolve().parents[1] / '.local' / 'credentials' / 'cookie_like.txt', encoding='utf-8').read().strip()

resp = requests.get(
    'https://www.douyin.com/aweme/v1/web/user/profile/self/',
    headers={
        'Cookie': cookie,
        'Referer': 'https://www.douyin.com/user/self?showSubTab=like',
    },
    params={
        'device_platform': 'webapp',
        'aid': '6383',
        'channel': 'channel_pc_web',
        'pc_client_type': '1',
        'version_code': '290100',
        'version_name': '29.1.0',
        'cookie_enabled': 'true',
        'screen_width': '1920',
        'screen_height': '1080',
        'browser_language': 'zh-CN',
        'browser_platform': 'Win32',
        'browser_name': 'Edge',
        'browser_version': '130.0.0.0',
        'browser_online': 'true',
        'engine_name': 'Blink',
        'engine_version': '130.0.0.0',
        'os_name': 'Windows',
        'os_version': '10',
        'cpu_core_num': '12',
        'device_memory': '8',
        'platform': 'PC',
        'downlink': '10',
        'effective_type': '4g',
        'round_trip_time': '100',
    },
    impersonate='edge101',
    timeout=15
)

print(f'Status: {resp.status_code}')
print(json.dumps(resp.json(), ensure_ascii=False, indent=2)[:3000])
