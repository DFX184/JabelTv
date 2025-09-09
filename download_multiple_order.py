from rich import print
from rich.console import Console
from rich.progress import (
    Progress, TextColumn, BarColumn,
    MofNCompleteColumn, TaskProgressColumn,
    TimeElapsedColumn, TimeRemainingColumn
)
import os.path as osp
from selenium.webdriver.common.by import By
from undetected_chromedriver import Chrome
import os
import re
import urllib
import requests
import m3u8
from Crypto.Cipher import AES
from concurrent.futures import ThreadPoolExecutor, as_completed
import ffmpeg
from selenium.webdriver.chrome.options import Options
import subprocess
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/78.0.3904.97 Safari/537.36"
    )
}
ROOT = "./"


def fetch_m3u8(driver, 
               folder_path,
               dir_name):
    html = driver.page_source
    result = re.search(r"https://.+?\.m3u8", html)
    if not result:
        return None, None, None

    m3u8_url = result[0]
    os.makedirs(folder_path, exist_ok=True)

    m3u8_file = os.path.join(folder_path, f"{dir_name}.m3u8")
    urllib.request.urlretrieve(m3u8_url, m3u8_file)

    m3u8_obj = m3u8.load(m3u8_file)
    ts_list = ["/".join(m3u8_url.split("/")[:-1]) + "/" + seg.uri for seg in m3u8_obj.segments]

    cipher = None
    for key in m3u8_obj.keys:
        if key:
            key_uri, key_iv = key.uri, key.iv
            m3u8_key_url = "/".join(m3u8_url.split("/")[:-1]) + "/" + key_uri
            resp = requests.get(m3u8_key_url, headers=headers, timeout=10)
            content_key = resp.content
            iv = bytes.fromhex(key_iv.replace("0x", "")) if key_iv else b"\x00" * 16
            cipher = AES.new(content_key, AES.MODE_CBC, iv)
            break

    return ts_list, cipher, folder_path


def _download_single_ts(ts_url, save_path, cipher=None):
    resp = requests.get(ts_url, headers=headers, timeout=20, stream=True)
    resp.raise_for_status()
    data = resp.content
    if cipher:
        data = cipher.decrypt(data)
    with open(save_path, "wb") as f:
        f.write(data)
    return save_path

def download_ts_files(
    ts_list,
    folder_path,
    cipher=None,
    progress=None,
    task_id=None,
    max_workers=os.cpu_count()
):
    
    ts_path_list = []
    existing = 0
    for idx, _ in enumerate(ts_list, start=1):
        dst = os.path.join(folder_path, f"{idx:05d}.ts")
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            existing += 1
            ts_path_list.append(dst)

    if progress and task_id is not None:
        progress.update(task_id, completed=existing,
                        status=f"续传：已存在 {existing} 个片段")

    futures = {}
    with ThreadPoolExecutor(max_workers=max_workers) as exe:
        for idx, ts_url in enumerate(ts_list, start=1):
            dst = os.path.join(folder_path, f"{idx:05d}.ts")

            if os.path.exists(dst) and os.path.getsize(dst) > 0:
                continue

            fut = exe.submit(_download_single_ts, ts_url, dst, cipher)
            futures[fut] = dst

        for fut in as_completed(futures):
            dst = futures[fut]
            try:
                path = fut.result()
                ts_path_list.append(path)
                if progress and task_id is not None:
                    progress.update(task_id, advance=1,
                                    status=f"完成 {os.path.basename(dst)}")
            except Exception as e:
                if progress and task_id is not None:
                    progress.update(task_id, advance=1,
                                    status=f"[red]失败 {os.path.basename(dst)}: {e.__class__.__name__}[/red]")

    for f in os.listdir(folder_path):
        if f.endswith(".m3u8"):
            os.remove(os.path.join(folder_path, f))
    return sorted(ts_path_list)

def merge_ts_raw(folder, output_file="output.ts"):
    ts_files = sorted([f for f in os.listdir(folder) if f.endswith(".ts")])
    # print(ts_files)
    with open(output_file, "wb") as out:
        for ts in ts_files:
            with open(os.path.join(folder, ts), "rb") as f:
                out.write(f.read())
    for ts in ts_files:
        os.remove(osp.join(folder,ts))


if __name__ == "__main__":
    codes = []
    
    while True:
        code = input("请输入影片的番号(q 退出):").lower()
        if code == "q":
            break
        codes.append(code)
    options = Options()
    # options.add_argument("--headless=new")  
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = Chrome()
    for code in codes:
        url = "https://jable.tv/videos/{}/"
        dest = url.format(code)
        driver.get(dest)
        try:
            title = driver.find_element(By.TAG_NAME,"h4")\
            .text
            print(f"爬取影片 {title}")
        except:
            print("输入番号错误!!!")
            continue
        folder_path = osp.join(ROOT,code)
        os.makedirs(folder_path,exist_ok=True)
        ts_list, cipher, folder_path = fetch_m3u8(driver,
            folder_path,
            code)
        with Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),                       
            TextColumn("个片段"),                        
            TaskProgressColumn(),                        
            TextColumn("完成"),                          
            TimeElapsedColumn(),                        
            TextColumn("已用时"),                       
            TimeRemainingColumn(),                     
            TextColumn("剩余"),                       
        ) as progress:
            task_id = progress.add_task(f"[cyan]{code} 下载中", total=len(ts_list))
            download_ts_files(ts_list,
                            folder_path,
                            cipher,
                            progress=progress,
                            task_id=task_id)
        merge_ts_raw(folder_path,output_file=osp.join(folder_path,"raw_" + code + ".ts"))
        
        input_file = osp.join(folder_path,"raw_" + code + ".ts")
        output_file = osp.join(folder_path, code + ".mp4")

        (
            ffmpeg
            .input(input_file)
            .output(
                output_file,
                **{
                    'c:v': 'h264_nvenc',   # 视频编码器
                    'b:v': '13000K',       # 码率
                    'threads': 5           # 线程数
                }
            )
            .global_args('-loglevel', 'quiet')
            .run()
        )
        os.remove(input_file)
