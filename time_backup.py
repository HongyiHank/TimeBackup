from __future__ import annotations

import tarfile
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable, Optional
from zipfile import ZIP_DEFLATED, ZipFile

from mcdreforged.api.all import (
    CommandSource,
    GreedyText,
    Info,
    Literal,
    PluginServerInterface,
    RColor,
    RequirementNotMet,
    RText,
    Serializable,
    UnknownArgument,
    new_thread,
)

PLUGIN_METADATA = {
    "id": "time_backup",
    'version': '1.0.0',
    'name': 'TimeBackup',
    "description": {
        "en_us": "A Minecraft Auto Backup Plugin",
        "zh_cn": "定時創建永久 zip 壓縮的備份"
    },
    'author': '猴貓<a102009102009@gmail.com>',
    'link': 'https://github.com/mc-cloud-town/TimeBackup'
}

STEP = 25
BASE_PATH = Path("")
CONFIG_FILE = "AutoPermanentBackup.json"

timer: Timer = None  # type: ignore
config: Configure
PREFIX = "!!auto-backup"
HELP_MESSAGE = """定時創建永久備份:
    !!auto-backup 幫助
    !!auto-backup status: 下次備份時間
    !!auto-backup enable: 開啟自動備份
    !!auto-backup disable: 關閉自動備份
    !!auto-backup make <備註(可選)>: 手動創建備份
"""


def convert_bytes(size: int):
    for x in ["bytes", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return "%3.1f %s" % (size, x)
        size /= 1024.0  # type: ignore


class Configure(Serializable):
    enabled: bool = True
    interval: str = "2d"  # s:second, m:minute, h:hour, d: day
    permission_requirement: int = 2
    saved_world_keywords: list[str] = [
        "Saved the game",  # 1.13+
        "Saved the world",  # 1.12-
    ]
    backup_path: str = "./permanent_backup"
    files_rules: list[str] = [
        "!__pycache__/**",
        "!test",
        "server/**/*",
    ]
    save_game_timeout: float = -1
    zip_type: str = "zip"  # zip, tar, tar.gz
    # 0:guest 1:user 2:helper 3:admin 4:owner
    minimum_permission_level: dict[str, int] = {}


def parse_paths(base_path: str | Path, rules: list[str]) -> list[Path]:
    base_path = Path(base_path)
    paths: list[Path] = []

    for rule in rules:
        pass_path = rule.startswith("!")
        if (path := Path(rule[1:])).is_file():
            if pass_path:
                if path in paths:
                    paths.remove(path)
                continue
            paths.append(path)
            continue

        for path in base_path.rglob(rule[1:] if pass_path else rule):
            if pass_path:
                if path in paths:
                    paths.remove(path)
                continue
            paths.append(path)

    return paths


def parse_interval(str_interval: str) -> int:
    digit, result = "", 0
    time_map = {"s": 1, "m": 60, "h": 3600, "d": 3600 * 24}

    def add(s: str = "") -> tuple[int, str]:
        return result + int(digit or 1) * time_map.get(s, 1), ""

    for s in str_interval + " ":
        if s.isdigit():
            digit += s
        elif s in time_map:
            result, digit = add(s)

    if digit:
        result, _ = add()

    return result


def format_file_name(file_name: str) -> str:
    for c in '/\\:*?"|<>':
        file_name = file_name.replace(c, "_")

    return file_name


class Timer:
    def __init__(self, server: PluginServerInterface) -> None:
        self.config = server.load_config_simple(
            CONFIG_FILE,
            target_class=Configure,
            in_data_folder=False,
        )
        self.server = server
        self._backup_ing = False
        self._saved_game_event = Event()
        self._stop_event = Event()
        self.creating_backup = Lock()
        self.last_backup_time = time.time()

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = enabled  # type: ignore
        self.server.save_config_simple(self.config, CONFIG_FILE, in_data_folder=False)

    def next_backup_message(self) -> str:
        if not self.config.enabled:  # type: ignore
            return "無 (已關閉自動備份)"
        return time.strftime(
            "%Y/%m/%d %H:%M:%S",
            time.localtime(self.last_backup_time + self.backup_interval),
        )

    def package_zip(
        self,
        filename: str,
        # (all: int, now: int) -> None
        callback: Callable[[int, int], None] | None = None,
    ) -> Path:
        rules, zip_path, zip_type, base_filename = (
            self.config.files_rules,  # type: ignore
            Path(self.config.backup_path),  # type: ignore
            self.config.zip_type,  # type: ignore
            format_file_name(filename),
        )
        zip_path.mkdir(parents=True, exist_ok=True)

        files = [
            path
            for path in parse_paths(
                BASE_PATH,
                [rules] if isinstance(rules, str) else rules,
            )
            # session.lock raise PermissionError
            if not str(path).endswith("session.lock")
        ]
        all_files = len(files)

        path, index = zip_path / f"{base_filename}.{zip_type}", 1
        while path.exists():
            index += 1
            path.with_name(f"{base_filename}.{index}.{zip_type}")

        if str(path).endswith(".tar.gz"):
            f = tarfile.open(path, "w:gz")
        elif str(path).endswith(".tar"):
            f = tarfile.open(path, "w")
        else:  # default use zip
            f = ZipFile(path, "w", ZIP_DEFLATED)

        for index, file in enumerate(files):
            try:
                if isinstance(f, tarfile.TarFile):
                    f.add(file)
                else:
                    f.write(file)
            except PermissionError:
                self.send(f"備份 {file} 無權限", broadcast=True)
            except Exception as e:
                self.server.logger.exception(e)

            if callback:
                callback(all_files, index + 1)

        f.close()
        return path

    def send(
        self,
        msg: str,
        *,
        source: CommandSource | None = None,
        broadcast=False,
    ) -> None:
        for line in msg.splitlines():
            text = f"§b[自動備份系統]§r {line}"

            if broadcast:
                self.server.broadcast(text)
            elif source:
                source.reply(text)

    def on_message(self, content: str) -> None:
        if (
            self._backup_ing
            and not self._saved_game_event.is_set()
            and content in self.config.saved_world_keywords  # type: ignore
        ):
            self._saved_game_event.set()

    @new_thread("time-backup")
    def create_backup(
        self,
        source: Optional[CommandSource] = None,
        ctx: dict = {},
        done_callback: Optional[Callable] = None,
    ) -> None:
        if self._backup_ing and source:
            self.send("§c正在備份中，請勿重複輸入§r", source=source)
            return

        if not self.config.enabled:  # type: ignore
            return

        start_time = time.time()
        self.creating_backup.acquire(blocking=False)
        self._backup_ing = True
        self.send("§6備份中...請稍後§r", broadcast=True)

        self.server.execute("save-off")
        self.server.execute("save-all flush")
        self._saved_game_event.clear()

        timeout = self.config.save_game_timeout  # type: ignore
        if not self._saved_game_event.wait(None if timeout < 0 else timeout):
            self.send("§c備份超時，暫停備份§r", broadcast=True)
            self._backup_ing = False
            return

        try:
            filename = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
            if comment := ctx.get("cmt"):
                filename += f"_{comment}"

            def send_now(all: int, now: int):
                if now % int(all / 8) == 0 or all == now:
                    step_int = int((step := now * 100 / all) / (100 / 10))
                    self.send(
                        f"[{'█'*step_int}{' '*(10-step_int)}] {step:04.1f}%" f" [{all}/{(all-now):03d}]",
                        broadcast=True,
                    )

            path = self.package_zip(filename, send_now)

            self.send(
                f"備份§a完成§r，耗時 {round(time.time() - start_time, 1)} 秒\n"
                f"共計 {convert_bytes(path.stat().st_size)}",
                broadcast=True,
            )
        except Exception as e:
            self.server.logger.exception(e)
            self.send("§c備份時發生錯誤，暫停備份§r", broadcast=True)
            self._backup_ing = False
        finally:
            self._backup_ing = False
            self.creating_backup.release()
            self.server.execute("save-on")
            if done_callback:
                done_callback()

    def start(self):
        Thread(target=self.loop).start()

    def stop(self):
        self._stop_event.set()

    @property
    def backup_interval(self):
        return parse_interval(self.config.interval)  # type: ignore

    def loop(self):
        while True:
            while True:
                if self._stop_event.wait(1):
                    return
                if not self.config.enabled:  # type: ignore
                    continue
                if time.time() - self.last_backup_time > self.backup_interval:
                    break

            if self.server.is_server_startup():
                try:
                    self.last_backup_time = time.time()
                    self.send("§6觸發定時備份...§r", broadcast=True)
                    self.create_backup(
                        done_callback=lambda: self.send(  # type: ignore
                            f"§6下次備份時間 {self.next_backup_message()}§r",
                            broadcast=True,
                        )
                    )
                except Exception as e:
                    self.server.logger.exception(e)


def on_load(server: PluginServerInterface, ord):
    global timer

    timer = Timer(server)
    server.register_help_message(PREFIX, "定時創建永久備份")

    server.register_command(
        Literal(PREFIX)
        .runs(lambda src: src.reply(HELP_MESSAGE))
        .then(
            Literal("status").runs(lambda src: src.reply(f"§6下次備份時間: {timer.next_backup_message()}§r"))
        )
        .requires(lambda src: src.has_permission(timer.config.permission_requirement))  # type: ignore
        .on_error(
            RequirementNotMet,
            lambda src: (src.reply(RText("權限錯誤", color=RColor.red))),
            handled=True,
        )
        .on_error(
            UnknownArgument,
            lambda src: src.reply(f"未知指令，輸入 {PREFIX} 查看幫助"),
        )
        .then(Literal("enable").runs(lambda src: (timer.set_enabled(True), src.reply("啟動自動備份"))))
        .then(Literal("disable").runs(lambda src: (timer.set_enabled(False), src.reply("關閉自動備份"))))
        .then(
            Literal("make")
            .runs(timer.create_backup)  # type: ignore
            .then(GreedyText("cmt").runs(timer.create_backup))  # type: ignore
        )  # type: ignore
    )
    timer.start()


def on_info(server: PluginServerInterface, info: Info) -> None:
    if not info.is_user and timer and info.content:
        timer.on_message(info.content)


def on_unload(server: PluginServerInterface) -> None:
    timer.stop()


def on_mcdr_stop(server: PluginServerInterface) -> None:
    timer.stop()
    if timer.creating_backup.locked():
        server.logger.info("Waiting for up to 300s for permanent backup to complete")
        if timer.creating_backup.acquire(timeout=300):
            timer.creating_backup.release()
