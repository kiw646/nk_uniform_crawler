import os
import json
import time
import hashlib
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image
from tqdm import tqdm
from ddgs import DDGS
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================================================
# 설정
# =========================================================

DATA_DIR = Path("/app/data")
RAW_DIR = DATA_DIR / "raw"

METADATA_FILE = DATA_DIR / "metadata.jsonl"
FAILURE_FILE = DATA_DIR / "failures.jsonl"
URL_HISTORY_FILE = DATA_DIR / "urls.txt"


# 최소 목표보다 넉넉하게 수집
TARGET_IMAGES = 20000

# 검색어 하나에서 페이지당 요청할 결과
RESULTS_PER_PAGE = 100

# 검색어 하나당 최대 검색 페이지
MAX_PAGES = 30

# 연속으로 새로운 URL이 없을 때 해당 검색어 종료
MAX_EMPTY_PAGES = 3

# 다운로드 timeout
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15

# 비정상적으로 큰 파일 방지
MAX_FILE_SIZE = 30 * 1024 * 1024   # 30 MB

# 검색엔진 요청 간 간격
SEARCH_DELAY = 1.5

# 이미지 다운로드 간 간격
DOWNLOAD_DELAY = 0.05


QUERIES = [
    # 한국어 - 기본
    "북한군 군복",
    "조선인민군 군복",
    "북한 군인 제복",
    "북한군 제복",
    "북한군 병사",
    "조선인민군 병사",

    # 한국어 - 군복/계급
    "북한군 장교 군복",
    "북한군 병사 군복",
    "북한군 전투복",
    "북한군 야전복",
    "북한군 겨울 군복",
    "북한군 여름 군복",
    "북한군 여성 군인",
    "북한군 장교",
    
    # 한국어 - 상황
    "북한군 열병식",
    "북한군 훈련",
    "북한군 행군",
    "북한군 경계 근무",
    "조선인민군 훈련",

    # 영어 - 기본
    "North Korean soldiers",
    "North Korean military uniform",
    "North Korean army uniform",
    "Korean People's Army uniform",
    "KPA soldiers",
    "KPA military uniform",

    # 영어 - 군복/계급
    "North Korean officer uniform",
    "North Korean soldier uniform",
    "North Korean combat uniform",
    "North Korean field uniform",
    "North Korean winter military uniform",
    "North Korean female soldiers",

    # 영어 - 상황
    "North Korean military parade",
    "North Korean soldiers training",
    "Korean People's Army soldiers",
    "Korean People's Army training",
]

# =========================================================
# 초기화
# =========================================================

RAW_DIR.mkdir(parents=True, exist_ok=True)


def create_session():
    """
    HTTP 연결 재사용 + 일시적인 오류 자동 재시도
    """

    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,

        backoff_factor=1,

        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],

        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=20,
        pool_maxsize=20,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })

    return session


SESSION = create_session()


# =========================================================
# 기록 관련
# =========================================================

def load_visited_urls():
    """
    이전 실행에서 이미 시도했던 URL을 불러온다.
    """

    if not URL_HISTORY_FILE.exists():
        return set()

    with open(URL_HISTORY_FILE, "r", encoding="utf-8") as f:
        return {
            line.strip()
            for line in f
            if line.strip()
        }


visited_urls = load_visited_urls()


def save_visited_url(url):
    with open(URL_HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(url + "\n")


def write_jsonl(path, data):
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                data,
                ensure_ascii=False
            ) + "\n"
        )


# =========================================================
# 이미지 관련
# =========================================================

FORMAT_EXTENSION = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tiff",
}


def get_existing_count():
    return sum(
        1
        for file in RAW_DIR.iterdir()
        if file.is_file()
    )


def validate_image(data):
    """
    실제로 Pillow가 열 수 있는 이미지인지 확인.

    여기서는 품질 필터링을 하지 않는다.
    """

    try:
        with Image.open(BytesIO(data)) as image:

            image.verify()

            image_format = image.format

            return image_format

    except Exception:
        return None


def generate_filename(url, image_format):
    """
    URL을 SHA256으로 바꿔 파일명으로 사용.

    이것은 이미지 내용의 중복 검사 목적이 아니다.
    """

    url_hash = hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()

    extension = FORMAT_EXTENSION.get(
        image_format,
        ".img"
    )

    return url_hash + extension


# =========================================================
# 다운로드
# =========================================================

def download_image(result, query):
    image_url = result.get("image")

    if not image_url:
        return False

    # 같은 URL 자체가 반복되는 경우만 차단
    if image_url in visited_urls:
        return False

    visited_urls.add(image_url)
    save_visited_url(image_url)

    try:

        response = SESSION.get(
            image_url,
            timeout=(
                CONNECT_TIMEOUT,
                READ_TIMEOUT
            ),
            stream=True,
        )

        response.raise_for_status()

        # -------------------------------------------------
        # Content-Type 확인
        # -------------------------------------------------

        content_type = response.headers.get(
            "Content-Type",
            ""
        ).lower()

        if not content_type.startswith("image/"):

            write_jsonl(
                FAILURE_FILE,
                {
                    "url": image_url,
                    "query": query,
                    "reason": "not_image_content_type",
                    "content_type": content_type,
                }
            )

            return False

        # -------------------------------------------------
        # 크기 제한
        # -------------------------------------------------

        content_length = response.headers.get(
            "Content-Length"
        )

        if content_length:

            try:
                if int(content_length) > MAX_FILE_SIZE:

                    write_jsonl(
                        FAILURE_FILE,
                        {
                            "url": image_url,
                            "query": query,
                            "reason": "file_too_large",
                        }
                    )

                    return False

            except ValueError:
                pass

        # -------------------------------------------------
        # 스트리밍 다운로드
        # -------------------------------------------------

        chunks = []
        downloaded_size = 0

        for chunk in response.iter_content(
            chunk_size=64 * 1024
        ):

            if not chunk:
                continue

            downloaded_size += len(chunk)

            if downloaded_size > MAX_FILE_SIZE:

                write_jsonl(
                    FAILURE_FILE,
                    {
                        "url": image_url,
                        "query": query,
                        "reason": "file_too_large_stream",
                    }
                )

                return False

            chunks.append(chunk)

        image_data = b"".join(chunks)

        # -------------------------------------------------
        # 이미지가 실제로 열리는지 확인
        # -------------------------------------------------

        image_format = validate_image(image_data)

        if image_format is None:

            write_jsonl(
                FAILURE_FILE,
                {
                    "url": image_url,
                    "query": query,
                    "reason": "invalid_image",
                }
            )

            return False

        # -------------------------------------------------
        # 파일 저장
        # -------------------------------------------------

        filename = generate_filename(
            image_url,
            image_format
        )

        path = RAW_DIR / filename

        with open(path, "wb") as f:
            f.write(image_data)

        # -------------------------------------------------
        # Metadata 저장
        # -------------------------------------------------

        metadata = {
            "filename": filename,
            "query": query,

            "image_url": image_url,

            # 이미지가 발견된 웹페이지
            "source_page": result.get("url"),

            "title": result.get("title"),

            "search_engine": result.get("source"),

            "reported_width": result.get("width"),
            "reported_height": result.get("height"),

            "content_type": content_type,

            "file_size": len(image_data),

            "download_time": time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        }

        write_jsonl(
            METADATA_FILE,
            metadata
        )

        return True

    except Exception as e:

        write_jsonl(
            FAILURE_FILE,
            {
                "url": image_url,
                "query": query,
                "reason": type(e).__name__,
                "message": str(e)[:500],
            }
        )

        return False


# =========================================================
# 검색
# =========================================================

def search_query(query, current_total):

    print()
    print("=" * 70)
    print(f"검색어: {query}")
    print("=" * 70)

    ddgs = DDGS(
        timeout=15
    )

    empty_pages = 0

    for page in range(
        1,
        MAX_PAGES + 1
    ):

        if current_total >= TARGET_IMAGES:
            break

        print(
            f"\n[{query}] page {page}"
        )

        try:

            results = ddgs.images(
                query=query,

                # 한국어/영어 검색을 모두 폭넓게 받기 위해
                # 지역 제한을 강하게 두지 않음
                region="wt-wt",

                safesearch="moderate",

                max_results=RESULTS_PER_PAGE,

                page=page,

                backend="auto",
            )

        except Exception as e:

            print(
                f"검색 실패: {type(e).__name__}: {e}"
            )

            time.sleep(5)
            continue

        if not results:

            empty_pages += 1

            if empty_pages >= MAX_EMPTY_PAGES:
                break

            continue

        new_url_count = 0

        progress = tqdm(
            results,
            desc=f"page {page}",
            unit="img"
        )

        for result in progress:

            if current_total >= TARGET_IMAGES:
                break

            image_url = result.get("image")

            if not image_url:
                continue

            if image_url in visited_urls:
                continue

            new_url_count += 1

            if download_image(
                result,
                query
            ):
                current_total += 1

            progress.set_postfix({
                "saved": current_total
            })

            time.sleep(DOWNLOAD_DELAY)

        # 검색 결과 대부분이 이전 URL이면
        # 해당 검색어가 거의 고갈됐다고 판단
        if new_url_count == 0:

            empty_pages += 1

        else:

            empty_pages = 0

        if empty_pages >= MAX_EMPTY_PAGES:

            print(
                "새로운 URL이 더 이상 거의 없어 "
                "다음 검색어로 이동합니다."
            )

            break

        time.sleep(SEARCH_DELAY)

    return current_total


# =========================================================
# MAIN
# =========================================================

def main():

    current_total = get_existing_count()

    print("=" * 70)
    print("IMAGE CRAWLER")
    print("=" * 70)

    print(f"현재 이미지 : {current_total}")
    print(f"목표 이미지 : {TARGET_IMAGES}")
    print(f"기존 URL    : {len(visited_urls)}")

    if current_total >= TARGET_IMAGES:

        print("이미 목표 수량에 도달했습니다.")
        return

    for query in QUERIES:

        current_total = search_query(
            query,
            current_total
        )

        if current_total >= TARGET_IMAGES:
            break

    print()
    print("=" * 70)
    print("크롤링 종료")
    print("=" * 70)
    print(f"최종 이미지 수: {current_total}")


if __name__ == "__main__":
    main()