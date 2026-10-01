"""AstrBot 禁漫天堂（JMComic）插件：下载本子 → 合并为 PDF → 发送给用户。

下载与 PDF 合并在独立子进程（jm_worker.py）里执行，子进程自带内存上限：
某本本子再大也只会终止那一次下载，不会拖垮 AstrBot。取消（/jm 暂停）
就是杀子进程，立即生效。
"""

import asyncio
import hashlib
import os
import re
import shutil
import sys

import yaml
from jmcomic import JmOption

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
OPTION_FILE = os.path.join(PLUGIN_DIR, "option.yml")
WORKER_FILE = os.path.join(PLUGIN_DIR, "jm_worker.py")

# 生成的 PDF 超过这个大小就不发送（平台对机器人发送的文件有大小上限）
MAX_PDF_MB = 100
# 单次下载硬超时：超过按失败终止，防止锁被一次卡死的下载永久占住
DOWNLOAD_TIMEOUT_SEC = 3600

# user_id -> 正在运行的下载子进程
_DOWNLOAD_PROCS: dict[str, asyncio.subprocess.Process] = {}
# user_id -> 下载互斥锁，防止同一用户并发下载互相覆盖文件
_USER_LOCKS: dict[str, asyncio.Lock] = {}
# user_id -> 用户已请求暂停，用于把退出码区分成"取消"而不是"失败"
_PAUSE_REQUESTED: set[str] = set()


def extract_integers(text: str) -> list[str]:
    """提取文本里的整数（保留原始写法，用于本子 ID）。"""
    return re.findall(r"-?\b\d+\b", text)


def extract_page_number(message_str: str, default: int = 1) -> int:
    """把消息里最后一个整数当作页码。"""
    numbers = re.findall(r"\d+", message_str)
    if not numbers:
        return default
    page = int(numbers[-1])
    return page if page > 0 else default


def split_keyword_and_page(message_str: str, command: str) -> tuple[str, int]:
    """把 "jms 全彩 2" 拆成 ("全彩", 2)：去掉命令本身，末尾整数是页码。

    关键词里的数字不会被删掉，"催眠術2" 这类关键词可以正常搜索。
    """
    text = message_str.strip()
    if text[: len(command)].lower() == command.lower():
        text = text[len(command) :].strip()

    match = re.search(r"(?:^|\s)(\d+)$", text)
    if match and match.start() > 0:
        return text[: match.start()].strip(), max(int(match.group(1)), 1)
    return text, 1


def safe_user_key(user_id: str) -> str:
    """把平台传来的 sender id 转成安全的目录名（同一个人始终对应同一个目录）。"""
    raw = str(user_id)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    readable = re.sub(r"[^0-9A-Za-z_.-]", "_", raw)[:48] or "user"
    return f"{readable}_{digest}"


def get_user_download_dir(user_id: str) -> str:
    """每个用户一个独立的临时下载目录。"""
    path = os.path.join(PLUGIN_DIR, "download", safe_user_key(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def prepare_download_dir(path: str) -> None:
    """清空临时目录。

    调用点保证此刻没有下载在写这个目录：下载任务开始时先清一次，
    下载结束后再清一次，不在下载过程中删文件。
    """
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        return

    for name in os.listdir(path):
        target = os.path.join(path, name)
        try:
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)
        except OSError as e:
            logger.warning(f"清理临时文件失败 {target}: {e}")


def find_generated_pdf(directory: str, album_id: str) -> str | None:
    """找到 img2pdf 插件生成的 PDF：优先 {album_id}.pdf，否则取第一个 PDF。"""
    if not os.path.isdir(directory):
        return None

    expected = os.path.join(directory, f"{album_id}.pdf")
    if os.path.isfile(expected):
        return expected

    for name in sorted(os.listdir(directory)):
        if name.lower().endswith(".pdf"):
            return os.path.join(directory, name)
    return None


def load_option_dict() -> dict:
    """读取插件目录下自己的 option.yml（账号、线程等配置的唯一来源）。"""
    with open(OPTION_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"option.yml 内容不是有效的配置: {OPTION_FILE}")
    return data


def build_query_client():
    """按 option.yml（含登录信息）构建客户端，供搜索 / 标签 / 排行榜使用。"""
    return JmOption.construct(load_option_dict()).new_jm_client()


def search_albums(keyword: str, page: int) -> list[tuple[str, str]]:
    """搜索本子，返回 [(id, 标题), ...]。"""
    page_result = build_query_client().search_site(search_query=keyword, page=page)
    return [(album_id, title) for album_id, title in page_result]


def fetch_ranking(monthly: bool, page: int) -> list[tuple[str, str]]:
    """获取月/周排行榜，返回 [(id, 标题), ...]。"""
    client = build_query_client()
    page_result = client.month_ranking(page=page) if monthly else client.week_ranking(page=page)
    return [(album_id, title) for album_id, title in page_result]


def fetch_album_meta(album_id: str) -> tuple[str, list[str], str]:
    """按 ID 查本子的标题、标签、作者。"""
    album = build_query_client().get_album_detail(album_id)
    tags = list(album.tags) if album.tags else []
    return album.title, tags, album.author or "Unknown Author"


@register("jm", "SpongeFun", "禁漫天堂插件：下载本子并合并成 PDF 发送", "1.2.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    @filter.command("jm")
    async def jm(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        # QQ 官方（群聊/单聊）不提供用户昵称，拿不到时统一用"你"兜底
        user_name = event.get_sender_name() or "你"
        message_str = event.message_str.strip()

        if re.fullmatch(r"jm\s*暂停", message_str, re.IGNORECASE):
            yield event.plain_result(await self._pause_download(user_id, user_name))
            return

        album_ids = extract_integers(message_str)
        if not album_ids:
            yield event.plain_result(f"{user_name}, 未找到有效的数字ID，请检查输入。例如：/jm 123456")
            return

        album_id = album_ids[0]
        lock = _USER_LOCKS.setdefault(user_id, asyncio.Lock())
        if lock.locked():
            yield event.plain_result(
                f"{user_name}，你还有一本正在下载，请等它结束，或发送 /jm 暂停 取消。"
            )
            return

        async with lock:
            user_dir = get_user_download_dir(user_id)
            try:
                yield event.plain_result(f"{user_name}, 正在查找 [{album_id}] !")
                await asyncio.to_thread(prepare_download_dir, user_dir)
                yield event.plain_result(
                    f"{user_name}，开始下载 [{album_id}]，大本子需要几分钟，"
                    f"期间可发送 /jm 暂停 取消。"
                )

                # 下载 + 合并 PDF 全在子进程里跑：
                # - 子进程自限内存（RLIMIT_AS），异常只损失本次下载
                # - 取消 = 终止子进程，立即生效
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, WORKER_FILE, album_id, user_dir,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _DOWNLOAD_PROCS[user_id] = proc
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), DOWNLOAD_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    _PAUSE_REQUESTED.discard(user_id)
                    await self._kill(proc)
                    yield event.plain_result(f"{user_name}，下载超过 1 小时，已强制终止。")
                    return

                if user_id in _PAUSE_REQUESTED:
                    _PAUSE_REQUESTED.discard(user_id)
                    yield event.plain_result(f"{user_name}，已暂停，本次下载取消。")
                    return

                if proc.returncode != 0:
                    err = (stderr or b"").decode("utf-8", "replace").strip()
                    err = err[-500:] if err else f"退出码 {proc.returncode}"
                    logger.error(f"用户{user_id}下载 {album_id} 失败：{err}")
                    yield event.plain_result(f"{user_name}，下载失败：{err}")
                    return

                pdf_path = await asyncio.to_thread(find_generated_pdf, user_dir, album_id)
                if not pdf_path:
                    yield event.plain_result(
                        f"{user_name}，下载完成但没有生成 PDF，请查看机器人日志里 jm_worker 的输出。"
                    )
                    return

                size_mb = os.path.getsize(pdf_path) / 1024 / 1024
                if size_mb > MAX_PDF_MB:
                    yield event.plain_result(
                        f"{user_name}，PDF 有 {size_mb:.1f} MB，超过 {MAX_PDF_MB} MB 上限，未发送。"
                    )
                    return

                yield event.plain_result(f"{user_name}，PDF已生成 ({size_mb:.1f} MB)，正在发送...")
                # 直接发文件并等它传完，之后才清理临时文件
                await event.send(
                    event.chain_result([File(name=f"{album_id}.pdf", file=pdf_path)])
                )
            except Exception as e:
                logger.error(f"用户{user_id}执行jm命令出错：{e}", exc_info=True)
                yield event.plain_result(f"{user_name}，操作出错：{e}")
            finally:
                _DOWNLOAD_PROCS.pop(user_id, None)
                _PAUSE_REQUESTED.discard(user_id)
                await asyncio.to_thread(prepare_download_dir, user_dir)

        if not lock.locked():
            _USER_LOCKS.pop(user_id, None)

    async def _pause_download(self, user_id: str, user_name: str) -> str:
        proc = _DOWNLOAD_PROCS.get(user_id)
        if proc is None:
            return f"{user_name}，当前没有正在进行的下载。"
        _PAUSE_REQUESTED.add(user_id)
        try:
            proc.terminate()
        except ProcessLookupError:
            pass

        # 子进程若卡在原生代码里收不到 SIGTERM，10 秒后升级为强杀
        async def _escalate():
            await asyncio.sleep(10)
            if proc.returncode is None:
                await self._kill(proc)

        asyncio.create_task(_escalate())
        return f"{user_name}，已暂停，正在停止本次下载并清理文件！"

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass

    @filter.command("jms")
    async def jms(self, event: AstrMessageEvent):
        user_name = event.get_sender_name() or "你"
        keyword, page = split_keyword_and_page(event.message_str, "jms")
        if not keyword:
            yield event.plain_result(f"{user_name}，请带上搜索关键词，例如：/jms 全彩 2")
            return

        yield event.plain_result(f"{user_name}, {keyword}这种题材实在是太涩啦!页面：{page}")
        try:
            albums = await asyncio.to_thread(search_albums, keyword, page)
        except Exception as e:
            logger.error(f"用户{event.get_sender_id()}执行jms命令出错：{e}", exc_info=True)
            yield event.plain_result(f"{user_name}, 搜索出错：{e}")
            return

        if not albums:
            yield event.plain_result(f"{user_name}，第 {page} 页没有搜到结果。")
            return

        yield event.plain_result("\n".join(f"[{aid}]: {title}" for aid, title in albums))

    @filter.command("jmtag")
    async def jmtag(self, event: AstrMessageEvent):
        user_name = event.get_sender_name() or "你"
        album_ids = extract_integers(event.message_str)
        if not album_ids:
            yield event.plain_result(f"{user_name}, 未找到有效的数字ID，请检查输入。例如：/jmtag 123456")
            return

        album_id = album_ids[0]
        try:
            title, tags, author = await asyncio.to_thread(fetch_album_meta, album_id)
        except Exception as e:
            logger.error(f"用户{event.get_sender_id()}执行jmtag命令出错：{e}", exc_info=True)
            yield event.plain_result(f"{user_name}, 获取标签时发生错误：{e}")
            return

        tags_str = ", ".join(tags) if tags else "无标签"
        yield event.plain_result(f"[{album_id}]:\n{title}\n作者: {author}\n标签: {tags_str}")

    @filter.command("jmmr")
    async def jm_monthly_ranking(self, event: AstrMessageEvent):
        async for result in self._ranking(event, "月度排行榜", monthly=True):
            yield result

    @filter.command("jmwr")
    async def jm_weekly_ranking(self, event: AstrMessageEvent):
        async for result in self._ranking(event, "周度排行榜", monthly=False):
            yield result

    async def _ranking(self, event: AstrMessageEvent, name: str, monthly: bool):
        user_name = event.get_sender_name() or "你"
        page = extract_page_number(event.message_str)
        yield event.plain_result(f"{user_name}，正在获取{name}第 {page} 页...")

        try:
            albums = await asyncio.to_thread(fetch_ranking, monthly, page)
        except Exception as e:
            logger.error(f"用户{event.get_sender_id()}获取{name}出错：{e}", exc_info=True)
            yield event.plain_result(f"{user_name}，获取{name}时发生错误：{e}")
            return

        if not albums:
            yield event.plain_result(f"{user_name}，未能获取到第 {page} 页的排行榜数据。")
            return

        body = "\n".join(f"[{aid}]: {title}" for aid, title in albums)
        yield event.plain_result(f"{name} 第 {page} 页:\n{body}")

    @filter.command("jmhelp")
    async def jm_help(self, event: AstrMessageEvent):
        user_name = event.get_sender_name() or "你"
        help_text = f"""
{user_name}，欢迎使用禁漫天堂插件！
以下是可用的命令列表：

/jm <ID>          - 下载指定 ID 的本子，合并成 PDF 后发送。
/jm 暂停          - 暂停当前正在进行的下载，并清理服务器上的临时文件。
/jms <关键词> [页码] - 搜索本子，页码写在最后，默认第 1 页。
/jmtag <ID>       - 查询指定 ID 本子的标签。
/jmmr [页码]      - 获取月度热门排行榜，默认第 1 页。
/jmwr [页码]      - 获取周度热门排行榜，默认第 1 页。
/jmhelp           - 显示此帮助信息。

注意：[] 表示可选参数。
        """.strip()
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件被卸载/停用时调用：终止还在跑的下载子进程。"""
        for user_id, proc in list(_DOWNLOAD_PROCS.items()):
            _PAUSE_REQUESTED.add(user_id)
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
        _DOWNLOAD_PROCS.clear()
        _USER_LOCKS.clear()
        _PAUSE_REQUESTED.clear()
