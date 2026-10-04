import json
import time
import hashlib
import threading
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image
from tqdm import tqdm
from ddgs import DDGS
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# =========================================================
# 기본 설정
# =========================================================

DATA_DIR = Path("/app/data")
RAW_DIR = DATA_DIR / "raw"

METADATA_FILE = DATA_DIR / "metadata.jsonl"
FAILURE_FILE = DATA_DIR / "failures.jsonl"
URL_HISTORY_FILE = DATA_DIR / "urls.txt"
CONTENT_HASH_FILE = DATA_DIR / "content_hashes.txt"

RAW_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# =========================================================
# 크롤링 설정
# =========================================================

TARGET_IMAGES = 20000

RESULTS_PER_PAGE = 100

MAX_PAGES = 30

MAX_EMPTY_PAGES = 3


# =========================================================
# 병렬 처리
# =========================================================

# 동시에 다운로드할 이미지 수
MAX_WORKERS = 8


# =========================================================
# 네트워크 설정
# =========================================================

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15

MAX_FILE_SIZE = 30 * 1024 * 1024

SEARCH_DELAY = 1.5

DOWNLOAD_DELAY = 0.05


# =========================================================
# 이미지 품질 기준
# =========================================================

MIN_WIDTH = 300
MIN_HEIGHT = 300

MAX_ASPECT_RATIO = 4.0


# =========================================================
# 쓰레기 이미지 키워드
# =========================================================

JUNK_KEYWORDS = [

    # 영어
    "drawing",
    "illustration",
    "illustrated",
    "vector",
    "clipart",
    "cartoon",
    "anime",
    "cosplay",
    "costume",
    "toy",
    "figure",
    "figurine",
    "game",
    "gaming",
    "skin",
    "3d render",
    "rendering",
    "painting",
    "sketch",
    "icon",
    "logo",

    # 한국어
    "그림",
    "일러스트",
    "벡터",
    "클립아트",
    "만화",
    "애니",
    "코스프레",
    "코스튬",
    "피규어",
    "장난감",
    "게임",
    "스킨",
    "렌더링",
    "도안",
    "아이콘",
    "로고",
    "이재명",
    "민주당",
    "트럼프",
    "일러",
    "국민의 힘",
    "좌파",
    "강신철",
    "김정은"
]


# =========================================================
# 검색어
# =========================================================

QUERIES = [

    # 한국어 - 기본
    "북한군 군복 사진",
    "북한군 군복",
    "북한군 열병식 군복 사진",

    # 영어 - 기본
    "North Korean soldiers photo",
    "KPA soldiers photo",


    # 영어 - 상황
    "North Korean military parade soldiers photo",
    "North Korean soldiers training photo",
]


# =========================================================
# 병렬 처리용 LOCK
# =========================================================

# 여러 스레드가 동시에 파일에 기록하는 것을 방지
WRITE_LOCK = threading.Lock()

# SHA256 set 동시 접근 방지
HASH_LOCK = threading.Lock()

# 각 Thread마다 독립적인 requests.Session 사용
THREAD_LOCAL = threading.local()


# =========================================================
# HTTP SESSION
# =========================================================

def create_session():

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
            504
        ],

        allowed_methods=["GET"]
    )

    adapter = HTTPAdapter(

        max_retries=retry,

        pool_connections=20,

        pool_maxsize=20
    )

    session.mount(
        "http://",
        adapter
    )

    session.mount(
        "https://",
        adapter
    )

    session.headers.update({

        "User-Agent": (

            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36"
        ),

        "Accept":
            "image/avif,"
            "image/webp,"
            "image/apng,"
            "image/*,"
            "*/*;q=0.8"
    })

    return session


def get_thread_session():

    """
    Thread마다 독립적인 Session 생성
    """

    if not hasattr(
        THREAD_LOCAL,
        "session"
    ):

        THREAD_LOCAL.session = (
            create_session()
        )

    return THREAD_LOCAL.session


# =========================================================
# JSONL 저장
# =========================================================

def write_jsonl(
    path,
    data
):

    with WRITE_LOCK:

        with open(
            path,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(

                json.dumps(
                    data,
                    ensure_ascii=False
                )

                + "\n"
            )


# =========================================================
# 방문 URL
# =========================================================

def load_visited_urls():

    if not URL_HISTORY_FILE.exists():

        return set()

    with open(
        URL_HISTORY_FILE,
        "r",
        encoding="utf-8"
    ) as f:

        return {

            line.strip()

            for line in f

            if line.strip()
        }


visited_urls = load_visited_urls()


def save_visited_url(
    url
):

    with WRITE_LOCK:

        with open(
            URL_HISTORY_FILE,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(
                url + "\n"
            )


# =========================================================
# SHA256 이미지 중복 관리
# =========================================================

def sha256_file(
    path
):

    hasher = hashlib.sha256()

    with open(
        path,
        "rb"
    ) as f:

        while True:

            chunk = f.read(
                1024 * 1024
            )

            if not chunk:
                break

            hasher.update(
                chunk
            )

    return hasher.hexdigest()


def load_content_hashes():

    hashes = set()

    # 이미 해시 DB가 있는 경우
    if CONTENT_HASH_FILE.exists():

        with open(
            CONTENT_HASH_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            return {

                line.strip()

                for line in f

                if line.strip()
            }


    # 기존 이미지가 있다면
    # 처음 한 번 SHA256 DB 생성
    files = [

        file

        for file in RAW_DIR.iterdir()

        if file.is_file()
    ]


    if files:

        print(
            f"기존 이미지 {len(files)}장의 "
            "SHA256 인덱스를 생성합니다."
        )

        with open(
            CONTENT_HASH_FILE,
            "a",
            encoding="utf-8"
        ) as hash_file:

            for file in tqdm(
                files,
                desc="hash index",
                unit="img"
            ):

                try:

                    content_hash = (
                        sha256_file(file)
                    )

                    if content_hash not in hashes:

                        hashes.add(
                            content_hash
                        )

                        hash_file.write(
                            content_hash + "\n"
                        )

                except Exception:

                    continue


    return hashes


content_hashes = load_content_hashes()


def save_content_hash(
    content_hash
):

    with WRITE_LOCK:

        with open(
            CONTENT_HASH_FILE,
            "a",
            encoding="utf-8"
        ) as f:

            f.write(
                content_hash + "\n"
            )


def claim_content_hash(
    content_hash
):

    """
    여러 Thread가 같은 이미지를 동시에 저장하지 못하게 한다.
    """

    with HASH_LOCK:

        if content_hash in content_hashes:

            return False

        content_hashes.add(
            content_hash
        )

        return True


def release_content_hash(
    content_hash
):

    with HASH_LOCK:

        content_hashes.discard(
            content_hash
        )


# =========================================================
# 이미지 포맷
# =========================================================

FORMAT_EXTENSION = {

    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tiff"
}


# =========================================================
# 현재 이미지 개수
# =========================================================

def get_existing_count():

    return sum(

        1

        for file in RAW_DIR.iterdir()

        if file.is_file()
    )


# =========================================================
# 숫자 변환
# =========================================================

def safe_int(
    value
):

    try:

        return int(value)

    except (
        TypeError,
        ValueError
    ):

        return 0


# =========================================================
# 검색결과 다운로드 전 필터
# =========================================================

def get_result_filter_reason(
    result
):

    title = str(
        result.get("title")
        or ""
    )

    source_page = str(
        result.get("url")
        or ""
    )

    image_url = str(
        result.get("image")
        or ""
    )


    searchable_text = (

        title
        + " "
        + source_page
        + " "
        + image_url

    ).lower()


    # -----------------------------------------------------
    # 쓰레기 키워드 검사
    # -----------------------------------------------------

    for keyword in JUNK_KEYWORDS:

        if keyword in searchable_text:

            return (
                f"junk_keyword:{keyword}"
            )


    # -----------------------------------------------------
    # 검색엔진이 알려준 크기 검사
    # -----------------------------------------------------

    width = safe_int(
        result.get("width")
    )

    height = safe_int(
        result.get("height")
    )


    if (
        width > 0
        and
        height > 0
    ):

        if (
            width < MIN_WIDTH
            or
            height < MIN_HEIGHT
        ):

            return (
                "reported_too_small"
            )


        aspect_ratio = (

            max(
                width,
                height
            )

            /

            min(
                width,
                height
            )
        )


        if (
            aspect_ratio
            >
            MAX_ASPECT_RATIO
        ):

            return (
                "reported_bad_aspect_ratio"
            )


    return None


# =========================================================
# 실제 이미지 검사
# =========================================================

def validate_image(
    data
):

    try:

        with Image.open(
            BytesIO(data)
        ) as image:

            image_format = (
                image.format
            )

            width, height = (
                image.size
            )

            image.verify()


        # -------------------------------------------------
        # 작은 이미지 제거
        # -------------------------------------------------

        if (
            width < MIN_WIDTH
            or
            height < MIN_HEIGHT
        ):

            return (
                None,
                None,
                None
            )


        # -------------------------------------------------
        # 배너 형태 제거
        # -------------------------------------------------

        aspect_ratio = (

            max(
                width,
                height
            )

            /

            min(
                width,
                height
            )
        )


        if (
            aspect_ratio
            >
            MAX_ASPECT_RATIO
        ):

            return (
                None,
                None,
                None
            )


        return (

            image_format,

            width,

            height
        )


    except Exception:

        return (
            None,
            None,
            None
        )


# =========================================================
# 파일명 생성
# =========================================================

def generate_filename(
    url,
    image_format
):

    url_hash = hashlib.sha256(

        url.encode(
            "utf-8"
        )

    ).hexdigest()


    extension = FORMAT_EXTENSION.get(

        image_format,

        ".img"
    )


    return (
        url_hash
        +
        extension
    )


# =========================================================
# 이미지 다운로드 Worker
# =========================================================

def download_image(
    result,
    query
):

    image_url = result.get(
        "image"
    )


    if not image_url:

        return False


    claimed_hash = None

    file_saved = False


    try:

        session = (
            get_thread_session()
        )


        if DOWNLOAD_DELAY > 0:

            time.sleep(
                DOWNLOAD_DELAY
            )


        # -------------------------------------------------
        # 다운로드
        # -------------------------------------------------

        response = session.get(

            image_url,

            timeout=(

                CONNECT_TIMEOUT,

                READ_TIMEOUT
            ),

            stream=True
        )


        response.raise_for_status()


        # -------------------------------------------------
        # Content-Type
        # -------------------------------------------------

        content_type = response.headers.get(

            "Content-Type",

            ""

        ).lower()


        if not content_type.startswith(
            "image/"
        ):


            write_jsonl(

                FAILURE_FILE,

                {

                    "url":
                        image_url,

                    "query":
                        query,

                    "reason":
                        "not_image_content_type",

                    "content_type":
                        content_type
                }
            )


            return False


        # -------------------------------------------------
        # 서버가 알려준 파일 크기
        # -------------------------------------------------

        content_length = response.headers.get(
            "Content-Length"
        )


        if content_length:

            try:

                if (
                    int(content_length)
                    >
                    MAX_FILE_SIZE
                ):


                    write_jsonl(

                        FAILURE_FILE,

                        {

                            "url":
                                image_url,

                            "query":
                                query,

                            "reason":
                                "file_too_large"
                        }
                    )


                    return False


            except ValueError:

                pass


        # -------------------------------------------------
        # Stream 방식 다운로드
        # -------------------------------------------------

        chunks = []

        downloaded_size = 0


        for chunk in response.iter_content(

            chunk_size=
                64 * 1024

        ):


            if not chunk:

                continue


            downloaded_size += (
                len(chunk)
            )


            if (
                downloaded_size
                >
                MAX_FILE_SIZE
            ):


                write_jsonl(

                    FAILURE_FILE,

                    {

                        "url":
                            image_url,

                        "query":
                            query,

                        "reason":
                            "file_too_large_stream"
                    }
                )


                return False


            chunks.append(
                chunk
            )


        image_data = b"".join(
            chunks
        )


        # -------------------------------------------------
        # 실제 이미지 검사
        # -------------------------------------------------

        (
            image_format,
            width,
            height

        ) = validate_image(
            image_data
        )


        if image_format is None:


            write_jsonl(

                FAILURE_FILE,

                {

                    "url":
                        image_url,

                    "query":
                        query,

                    "reason":
                        "invalid_or_low_quality_image"
                }
            )


            return False


        # -------------------------------------------------
        # 이미지 내용 SHA256
        # -------------------------------------------------

        content_hash = hashlib.sha256(

            image_data

        ).hexdigest()


        # -------------------------------------------------
        # 정확 중복 제거
        # -------------------------------------------------

        if not claim_content_hash(
            content_hash
        ):

            return False


        claimed_hash = (
            content_hash
        )


        # -------------------------------------------------
        # 파일명
        # -------------------------------------------------

        filename = generate_filename(

            image_url,

            image_format
        )


        path = (
            RAW_DIR
            /
            filename
        )


        # -------------------------------------------------
        # 이미지 저장
        # -------------------------------------------------

        with open(
            path,
            "wb"
        ) as f:

            f.write(
                image_data
            )


        file_saved = True


        # -------------------------------------------------
        # SHA256 기록
        # -------------------------------------------------

        save_content_hash(
            content_hash
        )


        # -------------------------------------------------
        # Metadata 저장
        # -------------------------------------------------

        metadata = {

            "filename":
                filename,

            "query":
                query,

            "image_url":
                image_url,

            "source_page":
                result.get("url"),

            "title":
                result.get("title"),

            "search_engine":
                result.get("source"),

            "reported_width":
                result.get("width"),

            "reported_height":
                result.get("height"),

            "actual_width":
                width,

            "actual_height":
                height,

            "content_type":
                content_type,

            "file_size":
                len(image_data),

            "content_sha256":
                content_hash,

            "download_time":
                time.strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
        }


        write_jsonl(

            METADATA_FILE,

            metadata
        )


        return True


    except Exception as e:


        # 저장 전에 문제가 발생했을 경우
        # hash 예약 취소
        if (
            claimed_hash is not None
            and
            not file_saved
        ):

            release_content_hash(
                claimed_hash
            )


        write_jsonl(

            FAILURE_FILE,

            {

                "url":
                    image_url,

                "query":
                    query,

                "reason":
                    type(e).__name__,

                "message":
                    str(e)[:500]
            }
        )


        return False


# =========================================================
# 검색
# =========================================================

def search_query(
    query,
    current_total
):

    print()

    print(
        "=" * 70
    )

    print(
        f"검색어: {query}"
    )

    print(
        "=" * 70
    )


    ddgs = DDGS(
        timeout=15
    )


    empty_pages = 0


    # =====================================================
    # 페이지 반복
    # =====================================================

    for page in range(

        1,

        MAX_PAGES + 1
    ):


        if (
            current_total
            >=
            TARGET_IMAGES
        ):

            break


        print(
            f"\n[{query}] page {page}"
        )


        # -------------------------------------------------
        # 검색
        # -------------------------------------------------

        try:

            results = list(

                ddgs.images(

                    query=query,

                    region="wt-wt",

                    safesearch="moderate",

                    # 실사 사진
                    type_image="photo",

                    # 큰 이미지
                    size="Large",

                    max_results=
                        RESULTS_PER_PAGE,

                    page=
                        page,

                    backend=
                        "auto"
                )
            )


        except Exception as e:


            print(

                f"검색 실패: "

                f"{type(e).__name__}: "

                f"{e}"
            )


            time.sleep(
                5
            )


            continue


        # -------------------------------------------------
        # 검색 결과가 없을 경우
        # -------------------------------------------------

        if not results:


            empty_pages += 1


            if (
                empty_pages
                >=
                MAX_EMPTY_PAGES
            ):

                break


            continue


        # =================================================
        # 다운로드 후보 선정
        # =================================================

        candidates = []

        filtered_count = 0


        for result in results:


            image_url = result.get(
                "image"
            )


            if not image_url:

                continue


            # ---------------------------------------------
            # 이미 처리한 URL
            # ---------------------------------------------

            if (
                image_url
                in
                visited_urls
            ):

                continue


            # 먼저 URL 예약
            # 같은 페이지에서 중복 submit 방지
            visited_urls.add(
                image_url
            )

            save_visited_url(
                image_url
            )


            # ---------------------------------------------
            # 다운로드 전 필터
            # ---------------------------------------------

            filter_reason = (
                get_result_filter_reason(
                    result
                )
            )


            if (
                filter_reason
                is not None
            ):

                filtered_count += 1

                continue


            candidates.append(
                result
            )


        # -------------------------------------------------
        # 목표 숫자를 초과해서 실행하지 않기
        # -------------------------------------------------

        remaining = (

            TARGET_IMAGES
            -
            current_total
        )


        if (
            len(candidates)
            >
            remaining
        ):

            candidates = (
                candidates[:remaining]
            )


        new_url_count = (
            len(candidates)
        )


        # =================================================
        # 병렬 다운로드
        # =================================================

        if candidates:


            with ThreadPoolExecutor(

                max_workers=
                    MAX_WORKERS

            ) as executor:


                futures = [

                    executor.submit(

                        download_image,

                        result,

                        query

                    )

                    for result in candidates
                ]


                progress = tqdm(

                    total=
                        len(futures),

                    desc=
                        f"page {page}",

                    unit=
                        "img"
                )


                # -----------------------------------------
                # 완료되는 순서대로 결과 처리
                # -----------------------------------------

                for future in as_completed(
                    futures
                ):


                    try:

                        success = (
                            future.result()
                        )


                        if success:

                            current_total += 1


                    except Exception as e:


                        write_jsonl(

                            FAILURE_FILE,

                            {

                                "query":
                                    query,

                                "reason":
                                    "worker_exception",

                                "message":
                                    str(e)[:500]
                            }
                        )


                    progress.update(
                        1
                    )


                    progress.set_postfix({

                        "saved":
                            current_total,

                        "filtered":
                            filtered_count,

                        "workers":
                            MAX_WORKERS
                    })


                progress.close()


        else:


            print(

                f"다운로드 후보 없음 "

                f"(필터링 {filtered_count}개)"
            )


        # =================================================
        # 검색어 고갈 판단
        # =================================================

        if (
            new_url_count
            ==
            0
        ):


            empty_pages += 1


        else:


            empty_pages = 0


        if (
            empty_pages
            >=
            MAX_EMPTY_PAGES
        ):


            print(

                "새로운 다운로드 후보가 "
                "더 이상 거의 없어 "
                "다음 검색어로 이동합니다."
            )


            break


        time.sleep(
            SEARCH_DELAY
        )


    return current_total


# =========================================================
# MAIN
# =========================================================

def main():

    current_total = (
        get_existing_count()
    )


    print(
        "=" * 70
    )

    print(
        "IMAGE CRAWLER - PARALLEL"
    )

    print(
        "=" * 70
    )


    print(
        f"현재 이미지 : "
        f"{current_total}"
    )

    print(
        f"목표 이미지 : "
        f"{TARGET_IMAGES}"
    )

    print(
        f"기존 URL    : "
        f"{len(visited_urls)}"
    )

    print(
        f"내용 해시   : "
        f"{len(content_hashes)}"
    )

    print(
        f"병렬 Worker : "
        f"{MAX_WORKERS}"
    )

    print(

        f"품질 기준   : "

        f"{MIN_WIDTH}x{MIN_HEIGHT} 이상 / "

        f"최대 비율 {MAX_ASPECT_RATIO}:1"
    )


    # -----------------------------------------------------
    # 이미 목표 달성
    # -----------------------------------------------------

    if (
        current_total
        >=
        TARGET_IMAGES
    ):


        print(
            "이미 목표 수량에 도달했습니다."
        )


        return


    # =====================================================
    # 검색어 반복
    # =====================================================

    for query in QUERIES:


        current_total = search_query(

            query,

            current_total
        )


        if (
            current_total
            >=
            TARGET_IMAGES
        ):

            break


    print()

    print(
        "=" * 70
    )

    print(
        "크롤링 종료"
    )

    print(
        "=" * 70
    )

    print(

        f"최종 이미지 수: "
        f"{current_total}"
    )


# =========================================================
# 실행
# =========================================================

if __name__ == "__main__":

    main()
