"""JMComic 下载子进程：由 main.py 启动，专职负责「下载 → 合并 PDF」。

设计要点：
- 独立进程：内存暴涨或卡死只损失这一次下载，AstrBot 主进程不受影响。
- 启动即自限内存（Linux 下 RLIMIT_AS），并降低调度优先级，把 CPU 让给机器人。
- 父进程 SIGTERM 即退出：取消 = 杀进程，立即生效；半成品文件由父进程统一清理。

用法: python jm_worker.py <album_id> <user_dir>
产物: <user_dir>/<album_id>.pdf
"""

import os
import sys


def _setup_worker_env() -> None:
    """限制内存、降低优先级：必须在 import jmcomic 之前执行。"""
    if sys.platform != "linux":
        return
    import resource

    limit_gb = float(os.environ.get("JM_WORKER_MEM_GB", "1.5"))
    limit = int(limit_gb * 1024**3)
    try:
        # RLIMIT_AS 限虚拟内存，比实际 RSS 大数倍；真实峰值实测约 300MB。
        # 超限进程会被内核终止，不会拖垮 AstrBot（目标机另有 4GB swap 兜底）。
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass
    try:
        os.nice(5)
    except OSError:
        pass


def _patch_jpeg_save() -> None:
    """把 jmcomic 的存图函数换成高质量 JPEG。

    配置 suffix: .jpg 后 jmcomic 用 PIL 解码 webp 再按扩展名重编码保存，
    而 PIL 的 image.save() 默认 JPEG 质量 75，对漫画线稿偏糊；这里改成 90。
    JPEG 不支持 alpha，遇到 RGBA/P 模式先转 RGB。
    """
    from PIL import Image
    from jmcomic.jm_toolkit import JmImageTool

    def save_image(cls, image: Image.Image, filepath: str) -> None:
        if filepath.lower().endswith((".jpg", ".jpeg")):
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(filepath, "JPEG", quality=90)
        else:
            image.save(filepath)

    JmImageTool.save_image = classmethod(save_image)


def main() -> None:
    _setup_worker_env()
    _patch_jpeg_save()

    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <album_id> <user_dir>", file=sys.stderr)
        sys.exit(2)
    album_id, user_dir = sys.argv[1], sys.argv[2]

    import yaml
    import jmcomic
    from jmcomic import JmOption

    option_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "option.yml")
    with open(option_file, encoding="utf-8") as f:
        option_dict = yaml.safe_load(f)

    option_dict.setdefault("dir_rule", {})["base_dir"] = user_dir
    # 图片只为合并 PDF 而下载，转完就删，避免同一份内容占两份磁盘
    option_dict.setdefault("plugins", {})["after_album"] = [
        {
            "plugin": "img2pdf",
            "kwargs": {
                "pdf_dir": user_dir,
                "filename_rule": "Aid",
                "delete_original_file": True,
            },
        }
    ]

    option = JmOption.construct(option_dict)
    jmcomic.download_album(album_id, option)


if __name__ == "__main__":
    main()
