import os
import time
import random
import logging
import requests

log = logging.getLogger(__name__)

THIRD_PARTY_TERABOXDL_URL = os.getenv("THIRD_PARTY_TERABOXDL_URL")
PROXY_URL = os.getenv("PROXY_URL")

def _get_video_metadata(terabox_url: str) -> dict:
    # Direct mode: call the third-party resolver directly (no PROXY_URL chain needed).
    base = THIRD_PARTY_TERABOXDL_URL or "https://www.teraboxdl.site/"
    endpoint = base.rstrip("/") + "/api/proxy"

    delay = random.uniform(0.1, 2.5)
    log.info(f"Retrieving video metadata from resolver (jitter delay: {delay:.2f}s)")
    time.sleep(delay)

    response = requests.post(endpoint, json={"url": terabox_url}, timeout=120)

    if response.status_code != 200:
        raise Exception(f"Resolver request failed with status code {response.status_code}")

    return response.json()

def _get_file_size_bytes(stream_download_url: str) -> int:
    try:
        response = requests.head(stream_download_url, allow_redirects=True)
        content_length = response.headers.get('Content-Length')
        if content_length is None:
            raise ValueError("Server did not provide Content-Length header.")
        
        return int(content_length)
    
    except Exception as e:
        print(f"Error: {e}")
        return 0


#!--------PUBLIC API------------

def get_video_info(terabox_url: str, is_hd: bool) -> dict:
    data = _get_video_metadata(terabox_url)

    # with open("example_response.json", "w") as f:
    #     json.dump(data, f, indent=2)

    if data.get("error"):
        raise Exception(data.get("message", "Unknown error in getting video metadata"))
    if "list" not in data or not data["list"]:
        raise Exception("Video list not found or empty in metadata response")

    file_info = data["list"][0]

    # Folder-aware: walk directories via ?dir= to find the first video file.
    VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
    def _is_video(f):
        return str(f.get("server_filename", "")).lower().endswith(VIDEO_EXTS)

    def _has_link(f):
        return bool(f.get("stream_url") or f.get("direct_link"))

    if not file_info.get("isdir") and not _is_video(file_info):
        # Root contains mixed files (e.g. images first) → pick first downloadable file
        for f in data["list"]:
            if _has_link(f):
                file_info = f
                break
        else:
            raise Exception(
                "No downloadable file found in this share link."
            )

    if file_info.get("isdir"):
        from urllib.parse import quote
        base_url = terabox_url.split("?")[0]
        candidates = [f for f in data["list"] if f.get("isdir")]
        found = None
        visited = 0
        queue = [c["path"] for c in candidates]
        while queue and not found and visited < 30:
            dirpath = queue.pop(0)
            visited += 1
            sub = _get_video_metadata(base_url + "?dir=" + quote(dirpath))
            for f in sub.get("list", []):
                if f.get("isdir"):
                    queue.append(f["path"])
                elif str(f.get("server_filename", "")).lower().endswith(
                        (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")):
                    found = f
                    break
        if not found:
            raise Exception(
                "No video file found inside this folder link. "
                "Share the link of the video FILE itself."
            )
        file_info = found

    if is_hd:
        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": int(file_info.get("size", 0)),
            "download_url": file_info.get("direct_link", ""),
        }
    else:
        download_url = file_info.get("stream_url", "")
        new_file_size = _get_file_size_bytes(download_url)

        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": new_file_size,
            "download_url": download_url,
        }
    
# if __name__ == "__main__":
#     data = get_video_metadata("https://1024terabox.com/s/1gvhn4oF65BbRvrA_fSsuWA")

#     with open("example_response.json", "w") as f:
#         json.dump(data, f, indent=2)