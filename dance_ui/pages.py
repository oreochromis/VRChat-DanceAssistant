"""Five-page information hierarchy for the desktop interface."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from . import __version__, i18n
from .widgets import Card, HeroCard, MetricCard, label, page, section_title, status_label, table


def button(text, callback, *, primary=False, link=False):
    control = QPushButton(text)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    control.setProperty("primary", primary)
    control.setProperty("link", link)
    control.clicked.connect(callback)
    return control


def row(*widgets, stretch=False):
    body = QWidget()
    layout = QHBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch()
    return body, layout


def field(title, control, description=""):
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(7)
    layout.addWidget(label(title))
    layout.addWidget(control)
    if description:
        layout.addWidget(label(description, muted=True, wrap=True))
    return body


class Pages:
    """Layout mixin; actions and state belong to MainWindow."""

    def build_run_page(self):
        area, body, box = page()
        box.addWidget(section_title("现在播放", "当前歌曲、房间队列与缓存状态"))
        self.hero = HeroCard()
        hero = self.hero
        top, top_box = row()
        self.song_title = label("等待进入跳舞房", wrap=True)
        self.song_title.setProperty("heroTitle", True)
        self.song_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top_box.addWidget(self.song_title, 1)
        self.song_state = status_label("等待检测")
        top_box.addWidget(self.song_state, 0, Qt.AlignmentFlag.AlignTop)
        hero.box.addWidget(top)
        self.song_meta = label("进入 PyPyDance / WannaDance / DuDu FitDance 房间后，这里会显示当前歌曲、点歌人和缓存位置。", muted=True, wrap=True)
        hero.box.addWidget(self.song_meta)
        box.addWidget(hero)

        self.vivid_metrics = QWidget()
        metrics_box = QHBoxLayout(self.vivid_metrics)
        metrics_box.setContentsMargins(0, 0, 0, 0)
        metrics_box.setSpacing(12)
        self.metric_hits = MetricCard("本地命中", "success")
        self.metric_misses = MetricCard("回源", "warning")
        self.metric_saved = MetricCard("节省流量", "highlight")
        self.metric_queue = MetricCard("后续队列", "secondary")
        for widget in (self.metric_hits, self.metric_misses, self.metric_saved, self.metric_queue):
            metrics_box.addWidget(widget, 1)
        self.vivid_metrics.hide()
        box.addWidget(self.vivid_metrics)

        cooldown = Card()
        heading, heading_box = row(label("CoolDown · 休息一下", subheading=True))
        heading_box.addStretch()
        self.cooldown_badge = status_label("未启用")
        heading_box.addWidget(self.cooldown_badge)
        cooldown.box.addWidget(heading)
        self.cooldown_hint = label("选择一个本地放松视频，在下一次视频请求时替换播放。", muted=True, wrap=True)
        cooldown.box.addWidget(self.cooldown_hint)
        actions, action_box = row()
        self.cooldown_file_label = label("内置默认放松视频", muted=True, wrap=True)
        action_box.addWidget(self.cooldown_file_label, 1)
        self.cooldown_button = button("选择放松视频", self.toggle_cooldown, primary=True)
        action_box.addWidget(self.cooldown_button)
        cooldown.box.addWidget(actions)
        box.addWidget(cooldown)

        next_card = Card()
        title, title_box = row(label("后续队列", subheading=True))
        title_box.addStretch()
        self.queue_count = label("尚无排队歌曲", muted=True)
        title_box.addWidget(self.queue_count)
        next_card.box.addWidget(title)
        self.queue_table = table(["顺序", "歌曲", "点歌人", "缓存状态"], 1)
        self.queue_table.setAccessibleName("接下来的歌曲")
        next_card.box.addWidget(self.queue_table)
        self.queue_empty = label("还没有后续歌曲。加入房间并点歌后，队列会自动更新。", muted=True, wrap=True)
        next_card.box.addWidget(self.queue_empty)
        box.addWidget(next_card)

        self.downloads_card = Card()
        title, title_box = row(label("缓存任务", subheading=True))
        title_box.addStretch()
        self.download_count = label("暂无任务", muted=True)
        title_box.addWidget(self.download_count)
        self.clear_history_button = button("收起已结束", self.clear_download_history, link=True)
        title_box.addWidget(self.clear_history_button)
        self.downloads_card.box.addWidget(title)
        self.download_table = table(["曲目 / 文件", "下载进度", "速度", "状态"], 0)
        self.download_table.setAccessibleName("缓存任务及结果")
        self.download_table.horizontalHeader().setMinimumSectionSize(100)
        self.downloads_card.box.addWidget(self.download_table)
        self.download_empty = label("当前没有缓存任务。后续歌曲会按预缓存设置准备。", muted=True, wrap=True)
        self.downloads_card.box.addWidget(self.download_empty)
        self.downloads_card.hide()
        box.addWidget(self.downloads_card)

        self.session_stats = QWidget()
        session_box = QHBoxLayout(self.session_stats)
        session_box.setContentsMargins(0, 0, 0, 0)
        session_box.setSpacing(8)
        session_box.addWidget(label("本次会话", muted=True))
        self.session_hits = label("本地命中 0 首")
        self.session_hits.setProperty("metricTone", "success")
        self.session_misses = label("回源 0 首")
        self.session_misses.setProperty("metricTone", "warning")
        self.session_saved = label("节省流量 0 MB")
        self.session_saved.setProperty("metricTone", "highlight")
        for widget in (self.session_hits, self.session_misses, self.session_saved):
            session_box.addWidget(widget)
        session_box.addStretch()
        box.addWidget(self.session_stats)
        box.addStretch()
        return area

    def build_minimal_page(self):
        body = QWidget()
        body.setAccessibleName("极简缓存页面")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(34, 24, 34, 24)
        outer.addStretch()
        card = Card()
        card.box.setContentsMargins(26, 22, 26, 22)
        card.box.setSpacing(14)
        heading, heading_box = row(label("缓存服务", heading=True))
        heading_box.addStretch()
        self.minimal_state = status_label("正在准备", "info")
        heading_box.addWidget(self.minimal_state)
        card.box.addWidget(heading)
        card.box.addWidget(label("当前歌曲", muted=True))
        self.minimal_song_title = label("等待进入跳舞房", heading=True, wrap=True)
        card.box.addWidget(self.minimal_song_title)
        self.minimal_song_meta = label("进入舞蹈房后自动使用本地缓存。", muted=True, wrap=True)
        card.box.addWidget(self.minimal_song_meta)
        stats, stats_box = row()
        self.minimal_hits = label("本地命中 0 首")
        self.minimal_hits.setProperty("strong", True)
        self.minimal_saved = label("节省流量 0 MB", muted=True)
        stats_box.addWidget(self.minimal_hits)
        stats_box.addStretch()
        stats_box.addWidget(self.minimal_saved)
        card.box.addWidget(stats)
        self.minimal_service_button = button("启动服务", self.toggle_service, primary=True)
        self.minimal_service_button.setMinimumHeight(44)
        card.box.addWidget(self.minimal_service_button)
        self.minimal_close_to_tray = QCheckBox("点 X 最小化到托盘，不退出软件")
        self.minimal_close_to_tray.setToolTip("开启后点击 X 收进系统托盘，缓存服务保持运行；双击托盘图标恢复窗口。")
        self.minimal_close_to_tray.toggled.connect(self.set_minimal_close_to_tray)
        card.box.addWidget(self.minimal_close_to_tray)
        outer.addWidget(card)
        outer.addStretch()
        return body

    def build_settings_page(self):
        area, body, box = page()
        box.addWidget(section_title("设置", "下载、放松视频、网络与启动"))
        language_card = Card()
        language_card.box.addWidget(label("界面语言", subheading=True))
        self.language = QComboBox()
        for language_code, language_name in (("zh", "简体中文"), ("zh_TW", "繁體中文"),
                                             ("en", "English"), ("ja", "日本語"), ("ko", "한국어")):
            self.language.addItem(language_name, language_code)
        self.language.currentIndexChanged.connect(lambda: self.mark_dirty("settings"))
        language_card.box.addWidget(self.language)
        language_card.box.addWidget(label("首次启动跟随电脑系统语言；之后可在这里切换并保存。", muted=True, wrap=True))
        box.addWidget(language_card)
        startup = Card()
        startup.box.addWidget(label("启动", subheading=True))
        self.steamvr_startup = QCheckBox("跟随 SteamVR 启动")
        self.steamvr_startup.setTristate(True)
        self.steamvr_startup.setCheckState(Qt.CheckState.PartiallyChecked)
        self.steamvr_startup.setEnabled(False)
        self.steamvr_startup.toggled.connect(self.mark_steamvr_dirty)
        startup.box.addWidget(self.steamvr_startup)
        self.steamvr_hint = label("尚未读取 SteamVR 启动设置。", muted=True, wrap=True)
        startup.box.addWidget(self.steamvr_hint)
        self.steamvr_apply_button = button("应用启动设置", self.apply_steamvr_startup, primary=True)
        self.steamvr_apply_button.setEnabled(False)
        self.steamvr_refresh_button = button("刷新状态", self.refresh_steamvr_startup)
        actions, _ = row(self.steamvr_apply_button, self.steamvr_refresh_button, stretch=True)
        startup.box.addWidget(actions)
        self.steamvr_detail = label(
            "仅在 Windows 生效。设置时请先启动 SteamVR，再应用启动设置。开启后 SteamVR 启动时通过计划任务静默拉起本程序，不会再弹管理员授权框；移动程序位置后会自动更新启动路径。关闭 SteamVR 不会关闭本程序。",
            muted=True, wrap=True)
        startup.box.addWidget(self.steamvr_detail)
        self.vr_overlay = QCheckBox("SteamVR 面板页")
        self.vr_overlay.setToolTip("开启后在 SteamVR 菜单中显示「VRChat 跳舞房助手」页面：查看当前歌曲、队列与缓存状态，可用激光指针操作。需先启动 SteamVR。")
        self.vr_overlay.toggled.connect(lambda: self.mark_dirty("settings"))
        startup.box.addWidget(self.vr_overlay)
        self.overlay_note = label("开启后应用设置即生效，无需重启服务。", muted=True, wrap=True)
        startup.box.addWidget(self.overlay_note)
        self.close_to_tray = QCheckBox("点 X 最小化到托盘，不退出软件")
        self.close_to_tray.setToolTip("开启后点击右上角 X 会把窗口收进系统托盘（屏幕右下角），缓存服务保持运行；双击托盘图标恢复窗口，右键托盘图标可选择退出。")
        self.close_to_tray.toggled.connect(lambda: self.mark_dirty("settings"))
        startup.box.addWidget(self.close_to_tray)
        self.auto_start_service = QCheckBox("打开软件时自动启动服务")
        self.auto_start_service.setToolTip("开启后每次启动软件即自动开启缓存服务，无需手动点「启动服务」；管理员权限不足或服务启动失败时会按原样提示，不会静默吞掉错误。")
        self.auto_start_service.toggled.connect(lambda: self.mark_dirty("settings"))
        startup.box.addWidget(self.auto_start_service)
        self.storage_location_box = QWidget()
        storage_location_layout = QVBoxLayout(self.storage_location_box)
        storage_location_layout.setContentsMargins(0, 0, 0, 0)
        storage_location_layout.setSpacing(7)
        storage_location_layout.addWidget(label("数据保存位置"))
        self.storage_mode = QComboBox()
        self.storage_mode.addItem("系统用户目录（推荐）：解压到任何位置都保留设置", "roaming")
        self.storage_mode.addItem("随程序目录（便携）：配置跟随软件文件夹一起搬走", "portable")
        self.storage_mode.setMinimumWidth(420)
        storage_location_layout.addWidget(self.storage_mode)
        self.storage_mode_hint = label("", muted=True, wrap=True)
        storage_location_layout.addWidget(self.storage_mode_hint)
        self.storage_mode_button = button("切换保存位置", self.apply_storage_mode)
        storage_location_layout.addWidget(self.storage_mode_button, 0, Qt.AlignmentFlag.AlignLeft)
        storage_location_layout.addWidget(label(
            "切换会把配置、编号映射、歌单直取表等数据文件复制到新位置（原位置文件保留不删），需先停止服务。",
            muted=True, wrap=True))
        startup.box.addWidget(self.storage_location_box)
        hosts_row, hosts_box = row()
        self.hosts_clean_row = hosts_row
        self.hosts_clean_button = button("清理 hosts 残留", self.clean_stale_hosts_clicked)
        hosts_box.addWidget(self.hosts_clean_button)
        self.hosts_clean_status = label("", muted=True, wrap=True)
        hosts_box.addWidget(self.hosts_clean_status, 1)
        startup.box.addWidget(hosts_row)
        self.hosts_clean_hint = label(
            "上次异常退出（强杀、断电）可能在系统 hosts 留下本程序的条目，导致不开软件时视频无法加载。服务启动时会自动清理；此处可手动清理，适合准备停用或卸载本软件时使用。",
            muted=True, wrap=True,
        )
        startup.box.addWidget(self.hosts_clean_hint)
        box.addWidget(startup)
        vrcx_sync = Card()
        self.vrcx_sync_card = vrcx_sync
        vrcx_sync.box.addWidget(label("VRCX 数据同步", subheading=True))
        vrcx_sync.box.addWidget(label(
            "立即同步当前账号昵称与 VRChat 头像、好友共同游玩统计和排名头像；手动同步会刷新全部好友头像，无需等待 24 小时缓存到期。",
            muted=True, wrap=True))
        self.vrcx_sync_button = button("立即同步 VRCX 数据", self.sync_vrcx_now, primary=True)
        self.vrcx_sync_status = label("软件也会在启动后及 VRCX 数据变化时自动同步。", muted=True, wrap=True)
        sync_row, sync_box = row(self.vrcx_sync_button, self.vrcx_sync_status)
        sync_box.setStretch(1, 1)
        vrcx_sync.box.addWidget(sync_row)
        self.raw_records_button = button("查看与编辑原始记录", self.open_record_editor)
        self.raw_records_button.setToolTip("查询、新增、编辑或删除软件副本中的位置、房间事件、点歌及实际播放记录")
        vrcx_sync.box.addWidget(self.raw_records_button, 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(vrcx_sync)
        general = Card()
        self.general_card = general
        general.box.addWidget(label("缓存行为", subheading=True))
        preload_row, preload_box = row(label("预缓存后续歌曲"))
        preload_box.addStretch()
        self.preload_count = QSpinBox()
        self.preload_count.setRange(0, 10)
        self.preload_count.setSuffix(" 首")
        self.preload_count.setMinimumWidth(110)
        self.preload_count.valueChanged.connect(lambda: self.mark_dirty("settings"))
        preload_box.addWidget(self.preload_count)
        general.box.addWidget(preload_row)
        general.box.addWidget(label("仅提前缓存可解析的 CDN 歌曲。设为 0 时不预缓存后续歌曲。", muted=True, wrap=True))
        self.preload_sequential = QCheckBox("预载按队列顺序逐首下载（推荐开启）")
        self.preload_sequential.setToolTip("开启后将按照房间歌曲列表顺序从前往后一首一首下，避免出现带宽不够导致歌曲已播到下一首，对应视频还没下完的惨案~")
        self.preload_sequential.toggled.connect(lambda: self.mark_dirty("settings"))
        general.box.addWidget(self.preload_sequential)
        self.max_parallel = QSpinBox()
        self.max_parallel.setRange(1, 8)
        self.max_parallel.setSuffix(" 个")
        self.max_parallel.setMinimumWidth(110)
        self.max_parallel.valueChanged.connect(lambda: self.mark_dirty("settings"))
        self.max_parallel_row = field(
            "预载并发下载数", self.max_parallel,
            "默认 2 个；关闭“按队列顺序逐首下载”后生效。线程过多可能分摊带宽或触发服务器限流。")
        general.box.addWidget(self.max_parallel_row)
        self.bundle_direct = QCheckBox("歌单直取模式（推荐开启）")
        self.bundle_direct.setToolTip("开启后可绕过 API 的跳转，解决 PyPyDance 官方曲库油管源歌曲(如song)的播放问题~")
        self.bundle_direct.toggled.connect(lambda: self.mark_dirty("settings"))
        general.box.addWidget(self.bundle_direct)
        self.youtube_enabled = QCheckBox("启用 YouTube 缓存（推荐开启）")
        self.youtube_enabled.setToolTip("关掉的话youtube加载问题就要自己想办法了~")
        self.youtube_enabled.toggled.connect(lambda: self.mark_dirty("settings"))
        general.box.addWidget(self.youtube_enabled)
        general.box.addWidget(label("更改此选项前请先停止服务，应用后再次启动。", muted=True, wrap=True))
        box.addWidget(general)

        cooldown = Card()
        cooldown.box.addWidget(label("CoolDown 放松视频", subheading=True))
        self.cooldown_path = QLineEdit()
        self.cooldown_path.setPlaceholderText("留空使用内置默认放松视频")
        self.cooldown_path.textChanged.connect(lambda: self.mark_dirty("settings"))
        self.default_cooldown_button = button("恢复默认视频", self.cooldown_path.clear)
        file_row, file_box = row(self.cooldown_path)
        file_box.setStretch(0, 1)
        cooldown.box.addWidget(file_row)
        cooldown_actions, _ = row(button("自己上传视频", self.browse_cooldown),
                                  button("选择曲库中视频", self.choose_cooldown_from_library),
                                  self.default_cooldown_button, stretch=True)
        cooldown.box.addWidget(cooldown_actions)
        self.cooldown_trigger = QComboBox()
        self.cooldown_trigger.addItem("播放至下一首歌时自动触发", "next")
        self.cooldown_trigger.addItem("通过点击房间“重新载入视频”按键触发", "reload")
        self.cooldown_trigger.currentIndexChanged.connect(lambda: self.mark_dirty("settings"))
        self.cooldown_trigger_row = field(
            "CoolDown 触发时机", self.cooldown_trigger,
            "下一首模式：播放至下一首歌时自动触发；刷新模式：点击房间“重新载入视频”后，由当前歌曲触发。两种模式均为一次性替换。")
        cooldown.box.addWidget(self.cooldown_trigger_row)
        cooldown.box.addWidget(label("默认使用随程序附带的放松视频，也可以自选本地 MP4。应用更改后在“运行”页启用；等待替换时可以取消，正在替换时可以退出。", muted=True, wrap=True))
        box.addWidget(cooldown)

        network = Card()
        self.network_card = network
        network.box.addWidget(label("CDN 网络", subheading=True))
        self.preferred_ip = QLineEdit()
        self.preferred_ip.setPlaceholderText("自动选择（留空）")
        self.preferred_ip.textChanged.connect(lambda: self.mark_dirty("settings"))
        network.box.addWidget(field("优选 CDN IP", self.preferred_ip, "填写公网 IPv4 地址；留空由程序自动选择。对 PyPyDance 和 WannaDance 同时生效，保存后用于后续连接。"))
        self.auto_prefer_cdn = QCheckBox("自动优选 CDN（启动测速，使用中持续变慢时后台复测）")
        self.auto_prefer_cdn.setToolTip("启动服务时自动测速；真实下载持续偏慢时会小样本复测，明显提升才用于后续请求，不中断当前连接。节点失效会回落默认节点，可随时关闭。")
        self.auto_prefer_cdn.toggled.connect(lambda: self.mark_dirty("settings"))
        network.box.addWidget(self.auto_prefer_cdn)
        self.speedtest_threads = QSpinBox()
        self.speedtest_threads.setRange(1, 8)
        self.speedtest_threads.setSuffix(" 个")
        self.speedtest_threads.setMinimumWidth(110)
        self.speedtest_threads.valueChanged.connect(lambda: self.mark_dirty("settings"))
        self.speedtest_threads_row = field(
            "测速并发线程数", self.speedtest_threads,
            "默认 3 个；仅影响 CDN 测速，不影响视频下载。线程过多会分摊带宽，建议根据网络情况调整。")
        network.box.addWidget(self.speedtest_threads_row)
        self.speedtest_candidates = QSpinBox()
        self.speedtest_candidates.setRange(2, 64)
        self.speedtest_candidates.setSuffix(" 个")
        self.speedtest_candidates.setMinimumWidth(110)
        self.speedtest_candidates.valueChanged.connect(lambda: self.mark_dirty("settings"))
        self.speedtest_candidates_row = field(
            "快速测速候选数量", self.speedtest_candidates,
            "默认 8 个；增加候选可提高发现新节点的概率，但会延长初筛并增加流量。")
        network.box.addWidget(self.speedtest_candidates_row)
        self.speedtest_sample_mb = QSpinBox()
        self.speedtest_sample_mb.setRange(1, 32)
        self.speedtest_sample_mb.setSuffix(" MB")
        self.speedtest_sample_mb.setMinimumWidth(110)
        self.speedtest_sample_mb.valueChanged.connect(lambda: self.mark_dirty("settings"))
        self.speedtest_sample_row = field(
            "每个节点测速样本", self.speedtest_sample_mb,
            "默认 8MB；较小样本可能低估持续下载速度，较大样本更稳定但更耗时。")
        network.box.addWidget(self.speedtest_sample_row)
        self.quick_scan_button = button("快速测速", lambda: self.start_scan(False))
        self.full_scan_button = button("扩大范围测速（抽样）", lambda: self.start_scan(True))
        self.full_scan_button.setToolTip("从更大范围随机抽样探测，再对较快节点完整测速；耗时较长，不会穷举全部 IP。")
        actions, _ = row(self.quick_scan_button, self.full_scan_button, stretch=True)
        network.box.addWidget(actions)
        self.scan_status = label("快速测速会从当前、DNS、收藏、历史与新节点中选取最多 8 个候选，先小样本初筛，再对前 4 名及当前 IP 下载 8MB 复测。", muted=True, wrap=True)
        network.box.addWidget(self.scan_status)
        self.scan_table = table(["IP 地址", "实测速度", "结果"], 0)
        self.scan_table.doubleClicked.connect(lambda: self.use_selected_ip())
        self.scan_table.hide()
        network.box.addWidget(self.scan_table)
        self.use_ip_button = button("使用选中的 IP", self.use_selected_ip)
        self.use_ip_button.hide()
        network.box.addWidget(self.use_ip_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.favorite_ip_button = button("收藏选中的 IP", self.favorite_selected_ip)
        self.favorite_ip_button.setToolTip("把测速结果表（含启动时自动测速）里选中的 IP 存入下方收藏列表。")
        network.box.addWidget(self.favorite_ip_button, 0, Qt.AlignmentFlag.AlignLeft)
        network.box.addWidget(label("常用 CDN 收藏列表", subheading=True))
        self.favorites_hint = label("", muted=True, wrap=True)
        network.box.addWidget(self.favorites_hint)
        self.favorites_table = table(["收藏的 IP 地址"], 0)
        self.favorites_table.doubleClicked.connect(lambda: self.use_favorite_ip())
        network.box.addWidget(self.favorites_table)
        self.favorite_scan_button = button("测速全部收藏", self.start_favorite_scan)
        favorite_actions, _ = row(self.favorite_scan_button,
                                  button("使用选中的收藏", self.use_favorite_ip),
                                  button("删除选中的收藏", self.remove_favorite_ip), stretch=True)
        network.box.addWidget(favorite_actions)

        box.addWidget(network)

        update = Card()
        update.box.addWidget(label("更新", subheading=True))
        version_row, version_box = row(label(f"当前版本 {__version__}", muted=True))
        version_box.addStretch()
        self.update_check_button = button("检查更新", self.check_updates_clicked)
        version_box.addWidget(self.update_check_button)
        update.box.addWidget(version_row)
        self.update_status = label("启动时会自动检查一次新版本，也可以随时手动检查。", muted=True, wrap=True)
        update.box.addWidget(self.update_status)
        link_row, link_box = row(label("开源免费，欢迎 Star 与反馈问题：", muted=True))
        link_box.addWidget(button("GitHub 项目地址", self.open_project_page, link=True))
        link_box.addStretch()
        update.box.addWidget(link_row)
        bilibili_row, bilibili_box = row(label("反馈问题渠道 2：bilibili", muted=True))
        bilibili_box.addWidget(button("@鲨鲨比亚Sharkspeare", self.open_bilibili_page, link=True))
        bilibili_box.addWidget(label("私信或软件相关视频评论区留言即可", muted=True))
        bilibili_box.addStretch()
        update.box.addWidget(bilibili_row)
        box.addWidget(update)

        contributors = Card()
        contributors.box.addWidget(label("Contributors", subheading=True))
        contributors.box.addWidget(label("鲨鲨比亚Sharkspeare　小余12", muted=True))
        box.addWidget(contributors)
        self.reset_row, reset_box = row(label("将全部选项恢复为默认设置，不会删除视频、统计数据和外观。", muted=True, wrap=False))
        self.reset_settings_button = button("全部恢复默认", self.reset_all_settings)
        self.reset_settings_button.setProperty("danger", True)
        reset_box.addStretch(1)
        reset_box.addWidget(self.reset_settings_button)
        box.addWidget(self.reset_row)
        self.vrcx_sync_card = vrcx_sync
        vrcx_sync.box.addWidget(self.storage_location_box)
        box.insertWidget(1, label("缓存与曲库", subheading=True))
        box.insertWidget(2, general)
        box.insertWidget(3, cooldown)
        self.vrcx_group_heading = label("VRCX 与统计数据", subheading=True)
        box.insertWidget(4, self.vrcx_group_heading)
        box.insertWidget(5, vrcx_sync)
        self.network_group_heading = label("网络与诊断", subheading=True)
        box.insertWidget(6, self.network_group_heading)
        box.insertWidget(7, network)
        box.insertWidget(8, label("外观与启动", subheading=True))
        box.insertWidget(9, startup)
        box.insertWidget(10, update)
        box.insertWidget(11, contributors)
        return self.with_apply_footer(area, "settings")

    def apply_row(self, section):
        body, layout = row()
        hint = label("已保存", muted=True, wrap=True)
        layout.addWidget(hint, 1)
        action = button("应用更改", lambda: self.apply_settings(section), primary=True)
        action.setEnabled(False)
        layout.addWidget(action)
        self.apply_buttons[section] = action
        self.apply_hints[section] = hint
        return body

    def with_apply_footer(self, area, section):
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(area, 1)
        actions = self.apply_row(section)
        actions.layout().setContentsMargins(26, 12, 26, 12)
        layout.addWidget(actions)
        return wrapper

    def _compression_resource_changed(self):
        if self._syncing or self._compression_resources_syncing:
            return
        self._compression_encoder_selection = "full"
        self._update_compression_assignment_hint()
        self.mark_dirty("storage")

    def _update_compression_assignment_hint(self):
        if not hasattr(self, "compression_assignment_hint"):
            return
        selected = [encoder for encoder, checkbox in self.compression_resource_checks.items()
                    if checkbox.isEnabled() and checkbox.isChecked()]
        if not selected:
            text = "请至少勾选一项当前可用的压缩资源。"
        elif self._compression_encoder_selection == "auto":
            text = "自动选择模式：每个任务会检测可用显卡，失败时回退 CPU。"
        else:
            parallel = self.compression_parallel.value()
            names = {"cpu": "CPU x264", "nvenc": "NVIDIA NVENC", "amf": "AMD AMF", "qsv": "Intel QSV"}
            assignments = [selected[index % len(selected)] for index in range(parallel)]
            assignment_text = " + ".join(
                i18n.tr(names[encoder]) + (f"×{assignments.count(encoder)}" if assignments.count(encoder) > 1 else "")
                for encoder in selected)
            if parallel >= len(selected):
                text = f"当前 {parallel} 路：{assignment_text}；已覆盖全部已选资源。"
            else:
                missing = " + ".join(i18n.tr(names[encoder]) for encoder in selected if encoder not in assignments)
                text = (f"当前 {parallel} 路：{assignment_text}；{missing} 未参与，"
                        f"增加至 {len(selected)} 路可覆盖全部已选资源。")
        self.compression_assignment_hint.setProperty("_i18n_original_text", text)
        self.compression_assignment_hint.setText(i18n.tr(text))

    def select_all_compression_resources(self):
        for checkbox in self.compression_resource_checks.values():
            if checkbox.isEnabled():
                checkbox.setChecked(True)
        self._compression_resource_changed()

    def render_compression_resources(self, available):
        available = set(available)
        self._compression_resources_syncing = True
        try:
            for encoder, checkbox in self.compression_resource_checks.items():
                usable = encoder in available
                checkbox.setEnabled(usable)
                checkbox.setToolTip("" if usable else "当前硬件或编码器不可用")
                if not usable:
                    checkbox.setChecked(False)
        finally:
            self._compression_resources_syncing = False
        self._update_compression_assignment_hint()

    def build_storage_page(self):
        area, body, box = page()
        self.storage_scroll_area = area
        box.addWidget(section_title("存储", "管理临时缓存与永久曲库"))
        self.directory_pending_hint = label("", muted=True, wrap=True)
        self.directory_pending_hint.hide()
        box.addWidget(self.directory_pending_hint)
        self.library_structure = QComboBox()
        self.library_structure.addItem("根目录平铺（默认）", "flat")
        self.library_structure.addItem("按官方分类文件夹", "classified")
        self.library_structure.currentIndexChanged.connect(lambda: self.mark_dirty("storage"))
        self.library_structure_row, _ = row(label("曲库保存结构"), self.library_structure, stretch=True)
        box.addWidget(self.library_structure_row)
        cache = Card()
        cache.box.addWidget(label("临时缓存", subheading=True))
        self.cache_summary = label("暂无历史统计")
        self.cache_summary.setProperty("number", True)
        cache.box.addWidget(self.cache_summary)
        self.cache_usage = QProgressBar()
        self.cache_usage.setRange(0, 1000)
        self.cache_usage.setValue(0)
        self.cache_usage.setTextVisible(False)
        cache.box.addWidget(self.cache_usage)
        self.cache_detail = label("按缓存目录中的 MP4 文件统计", muted=True)
        cache.box.addWidget(self.cache_detail)
        self.cache_path = QLineEdit()
        self.cache_path.setPlaceholderText("选择临时缓存目录")
        self.cache_path.textChanged.connect(lambda: self.mark_dirty("storage"))
        self.cache_browse_button = button("选择目录", self.browse_cache)
        path_row, path_box = row(self.cache_path, self.cache_browse_button, button("打开目录", self.open_cache))
        path_box.setStretch(0, 1)
        cache.box.addWidget(field("缓存目录", path_row, "运行中可直接更改；有下载或曲库任务进行时会被暂时拒绝，稍后再试即可。"))
        controls, controls_box = row(label("缓存上限"))
        self.cache_limit = QSpinBox()
        self.cache_limit.setRange(100, 2147483647)
        self.cache_limit.setSingleStep(256)
        self.cache_limit.setSuffix(" MB")
        self.cache_limit.setMinimumWidth(140)
        self.cache_limit.valueChanged.connect(lambda: self.mark_dirty("storage"))
        controls_box.addWidget(self.cache_limit)
        controls_box.addStretch()
        self.clear_cache_button = button("清理临时缓存", self.clear_cache)
        self.clear_cache_button.setProperty("danger", True)
        controls_box.addWidget(self.clear_cache_button)
        cache.box.addWidget(controls)
        cache.box.addWidget(label("达到上限时，缓存会自动按最近使用情况清理。正在写入的文件会保留。", muted=True, wrap=True))
        box.addWidget(cache)

        self.organize_buttons = {}
        self.flatten_buttons = {}
        library = Card()
        library.box.addWidget(label("永久曲库 · PyPyDance", subheading=True))
        self.library_summary = label("尚未启用永久保存")
        self.library_summary.setProperty("number", True)
        library.box.addWidget(self.library_summary)
        self.library_path = QLineEdit()
        self.library_path.setPlaceholderText("选择 PyPyDance 永久保存目录")
        self.library_path.textChanged.connect(lambda: self.mark_dirty("storage"))
        path_row, path_box = row(self.library_path, button("选择目录", self.browse_library))
        path_box.setStretch(0, 1)
        library.box.addWidget(field("曲库目录", path_row, "运行中可直接更改；有下载或曲库任务进行时会被暂时拒绝，稍后再试即可。"))
        self.cdn_permanent = QCheckBox("永久保存 CDN 视频")
        self.youtube_permanent = QCheckBox("永久保存 YouTube 视频")
        self.cdn_permanent.toggled.connect(lambda: self.mark_dirty("storage"))
        self.youtube_permanent.toggled.connect(lambda: self.mark_dirty("storage"))
        library.box.addWidget(self.cdn_permanent)
        library.box.addWidget(self.youtube_permanent)
        library.box.addWidget(label("歌单直取开启时，YouTube 源会优先尝试 CDN 同名文件并参与预载；CDN 无文件时再于实际播放后录制。"
                                    "永久保存需先填写曲库目录才会生效，未填写时自动暂缓。", muted=True, wrap=True))
        library.box.addWidget(button("浏览曲库…", lambda: self.open_library_browser("pypy"), link=True), 0, Qt.AlignmentFlag.AlignLeft)
        self.organize_buttons["pypy"] = button("整理为分类文件夹…", lambda: self.start_library_organization("pypy"), link=True)
        library.box.addWidget(self.organize_buttons["pypy"], 0, Qt.AlignmentFlag.AlignLeft)
        self.flatten_buttons["pypy"] = button("整理为根目录平铺…", lambda: self.start_library_flatten("pypy"), link=True)
        library.box.addWidget(self.flatten_buttons["pypy"], 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(library)

        wd_library = Card()
        wd_library.box.addWidget(label("永久曲库 · WannaDance", subheading=True))
        self.wd_library_summary = label("尚未设置曲库目录")
        self.wd_library_summary.setProperty("number", True)
        wd_library.box.addWidget(self.wd_library_summary)
        self.wd_library_path = QLineEdit()
        self.wd_library_path.setPlaceholderText("选择 WannaDance 永久保存目录")
        self.wd_library_path.textChanged.connect(lambda: self.mark_dirty("storage"))
        wd_row, wd_row_box = row(self.wd_library_path, button("选择目录", self.browse_wd_library))
        wd_row_box.setStretch(0, 1)
        wd_library.box.addWidget(field("曲库目录", wd_row, "与 PyPyDance 曲库分开存放，文件按 wd_编号.mp4 命名。"))
        self.wd_permanent = QCheckBox("永久保存 WannaDance 视频")
        self.wd_permanent.toggled.connect(lambda: self.mark_dirty("storage"))
        wd_library.box.addWidget(self.wd_permanent)
        wd_library.box.addWidget(label("永久保存需先填写曲库目录才会生效，未填写时自动暂缓。"
                                       "永久曲库不受临时缓存上限影响。", muted=True, wrap=True))
        wd_library.box.addWidget(button("浏览曲库…", lambda: self.open_library_browser("wd"), link=True), 0, Qt.AlignmentFlag.AlignLeft)
        self.organize_buttons["wd"] = button("整理为分类文件夹…", lambda: self.start_library_organization("wd"), link=True)
        wd_library.box.addWidget(self.organize_buttons["wd"], 0, Qt.AlignmentFlag.AlignLeft)
        self.flatten_buttons["wd"] = button("整理为根目录平铺…", lambda: self.start_library_flatten("wd"), link=True)
        wd_library.box.addWidget(self.flatten_buttons["wd"], 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(wd_library)

        dudu_library = Card()
        dudu_library.box.addWidget(label("永久曲库 · DuDu FitDance", subheading=True))
        self.dudu_library_summary = label("尚未设置曲库目录")
        self.dudu_library_summary.setProperty("number", True)
        dudu_library.box.addWidget(self.dudu_library_summary)
        self.dudu_library_path = QLineEdit()
        self.dudu_library_path.setPlaceholderText("选择 DuDu FitDance 永久保存目录")
        self.dudu_library_path.textChanged.connect(lambda: self.mark_dirty("storage"))
        dudu_row, dudu_row_box = row(self.dudu_library_path, button("选择目录", self.browse_dudu_library))
        dudu_row_box.setStretch(0, 1)
        dudu_library.box.addWidget(field("曲库目录", dudu_row, "视频按 dudu_编号.mp4 保存，与另外两种曲库分开。"))
        self.dudu_permanent = QCheckBox("永久保存 DuDu FitDance 视频")
        self.dudu_permanent.toggled.connect(lambda: self.mark_dirty("storage"))
        dudu_library.box.addWidget(self.dudu_permanent)
        dudu_library.box.addWidget(button("浏览曲库…", lambda: self.open_library_browser("dudu"), link=True), 0, Qt.AlignmentFlag.AlignLeft)
        self.dudu_event_mode = QComboBox()
        self.dudu_event_mode.addItem("发现新活动时询问", "ask")
        self.dudu_event_mode.addItem("发现新活动时自动缓存", "auto")
        self.dudu_event_mode.addItem("关闭新活动检测", "off")
        self.dudu_event_mode.currentIndexChanged.connect(lambda: self.mark_dirty("storage"))
        dudu_library.box.addWidget(field(
            "活动歌单预载策略", self.dudu_event_mode,
            "启用 DuDu 证书后，每次启动软件后台检查；已有歌曲不重复下载，临时活动文件至少保留到活动次日 00:00。"))
        self.dudu_event_status = label("", muted=True, wrap=True)
        dudu_library.box.addWidget(self.dudu_event_status)
        self.dudu_event_api_url = QLineEdit()
        self.dudu_event_api_url.setPlaceholderText("https://api.dudufit.dance/api/v1/managed-playlists/ddfd")
        self.dudu_event_api_url.textChanged.connect(lambda: self.mark_dirty("storage"))
        self.dudu_event_api_reset_button = button("恢复默认接口", self.reset_dudu_event_api)
        api_row, api_row_box = row(self.dudu_event_api_url, self.dudu_event_api_reset_button)
        api_row_box.setStretch(0, 1)
        self.dudu_event_api_row = field(
            "歌单接口", api_row,
            "默认读取 DuDu 官方活动接口；可改为其他 HTTPS 地址。恢复默认后需点击“应用更改”。")
        dudu_library.box.addWidget(self.dudu_event_api_row)
        self.dudu_event_allow_over_limit = QCheckBox("允许活动歌曲在保留期内超过临时缓存上限")
        self.dudu_event_allow_over_limit.toggled.connect(lambda: self.mark_dirty("storage"))
        self.dudu_event_over_limit_row = field(
            "活动缓存超限（高级）", self.dudu_event_allow_over_limit,
            "关闭时先清理普通缓存，仍不足则暂停活动预载并提示；正常播放下载优先，未到期活动不会被提前删除。")
        dudu_library.box.addWidget(self.dudu_event_over_limit_row)
        self.organize_buttons["dudu"] = button("整理为分类文件夹…", lambda: self.start_library_organization("dudu"), link=True)
        dudu_library.box.addWidget(self.organize_buttons["dudu"], 0, Qt.AlignmentFlag.AlignLeft)
        self.flatten_buttons["dudu"] = button("整理为根目录平铺…", lambda: self.start_library_flatten("dudu"), link=True)
        dudu_library.box.addWidget(self.flatten_buttons["dudu"], 0, Qt.AlignmentFlag.AlignLeft)
        self.dudu_setup_button = button("启用 DuDu FitDance 缓存…", self.setup_dudu, primary=True)
        dudu_library.box.addWidget(self.dudu_setup_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.dudu_setup_status = label("首次启用会生成并信任本机开发证书；之后自动复用。", muted=True, wrap=True)
        dudu_library.box.addWidget(self.dudu_setup_status)
        box.addWidget(dudu_library)
        for flatten_button in self.flatten_buttons.values():
            flatten_button.setToolTip("整理后删除空分类文件夹；含其他文件的文件夹会保留。")

        compression = Card()
        self.compression_card = compression
        compression.box.addWidget(label("曲库压缩", subheading=True))
        compression.box.addWidget(label(
            "仅处理永久曲库，保持分辨率、帧率和实际帧数；新文件至少小 25% 才替换原片，验证失败不会修改原文件。",
            muted=True, wrap=True))
        self._compression_encoder_selection = "cpu"
        self._compression_resources_syncing = False
        self.compression_resource_checks = {}
        resource_widgets = [label("可用资源")]
        for value, text in (("cpu", "CPU x264"), ("nvenc", "NVIDIA NVENC"),
                            ("amf", "AMD AMF"), ("qsv", "Intel QSV")):
            checkbox = QCheckBox(text)
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._compression_resource_changed)
            self.compression_resource_checks[value] = checkbox
            resource_widgets.append(checkbox)
        self.compression_browse_button = button("浏览并选择歌曲…", self.open_compression_library_browser)
        self.compression_browse_row, _ = row(self.compression_browse_button, stretch=True)
        compression.box.addWidget(self.compression_browse_row)
        resource_widgets.append(button("全部勾选", self.select_all_compression_resources))
        self.compression_resources_row, _ = row(*resource_widgets, stretch=True)
        compression.box.addWidget(self.compression_resources_row)
        self.compression_crf = QComboBox()
        self.compression_crf.addItem("质量档（更清晰，文件通常较大）", "quality")
        self.compression_crf.addItem("体积档（更省空间）", "volume")
        self.compression_crf.setToolTip("自动使用适合当前编码器的参数；无需了解 CRF、CQ、QP 或 ICQ。")
        self.compression_crf.currentIndexChanged.connect(lambda: self.mark_dirty("storage"))
        self.compression_parallel = QSpinBox()
        self.compression_parallel.setRange(1, 8)
        self.compression_parallel.setValue(1)
        self.compression_parallel.setToolTip("同时压缩的视频总数，可设为 1–8。多选硬件时按 CPU、NVIDIA、AMD、Intel 顺序循环分配；达到资源数量后覆盖全部设备，继续增加则重复分配。")
        self.compression_parallel.valueChanged.connect(lambda: self.mark_dirty("storage"))
        self.compression_parallel.valueChanged.connect(self._update_compression_assignment_hint)
        crf_row, crf_box = row(label("压缩档位"), self.compression_crf,
                               label("并行任务"), self.compression_parallel, stretch=True)
        compression.box.addWidget(crf_row)
        self.compression_assignment_hint = label("", muted=True, wrap=True)
        compression.box.addWidget(self.compression_assignment_hint)
        self._update_compression_assignment_hint()
        self.compression_auto_game = QCheckBox("VRChat 启动时自动暂停，退出后自动继续")
        self.compression_auto_game.setToolTip("手动暂停优先级更高；手动暂停后不会因 VRChat 退出而自动恢复。")
        self.compression_auto_game.toggled.connect(lambda: self.mark_dirty("storage"))
        compression.box.addWidget(self.compression_auto_game)
        self.compression_start_button = button("开始压缩", self.start_compression, primary=True)
        self.compression_start_button.setEnabled(False)
        self.compression_pause_button = button("暂停", self.pause_compression)
        self.compression_resume_button = button("继续", self.resume_compression)
        self.compression_cancel_button = button("取消任务", self.cancel_compression)
        compression_actions, _ = row(self.compression_start_button, self.compression_pause_button,
                                     self.compression_resume_button, self.compression_cancel_button, stretch=True)
        compression.box.addWidget(compression_actions)
        self.compression_progress = QProgressBar()
        self.compression_progress.setRange(0, 100)
        self.compression_progress.setValue(0)
        self.compression_progress.setTextVisible(False)
        compression.box.addWidget(self.compression_progress)
        self.compression_status = label("尚未开始压缩", muted=True, wrap=True)
        compression.box.addWidget(self.compression_status)
        self.compression_current = label("", muted=True, wrap=True)
        compression.box.addWidget(self.compression_current)
        self.compression_details_button = button("处理详情", self.toggle_compression_details, link=True)
        compression.box.addWidget(self.compression_details_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.compression_details = QPlainTextEdit()
        self.compression_details.setReadOnly(True)
        self.compression_details.setMaximumHeight(220)
        self.compression_details.hide()
        compression.box.addWidget(self.compression_details)
        box.addWidget(compression)

        maintenance = Card()
        self.maintenance_card = maintenance
        maintenance.box.addWidget(label("曲库维护", subheading=True))
        maintenance.box.addWidget(label("PyPyDance 需要编号索引；WannaDance 和 DuDu FitDance 文件名自带编号，无需重建索引。", muted=True, wrap=True))
        self.rebuild_button = button("重建 PyPyDance 索引", lambda: self.start_maintenance("rebuild"))
        self.maintenance_rebuild_row, _ = row(self.rebuild_button, stretch=True)
        maintenance.box.addWidget(self.maintenance_rebuild_row)
        self.maintenance_verify_note = label("校验支持三种曲库；已压缩文件跳过官方原片大小比较，大小不同不代表文件损坏。", muted=True, wrap=True)
        maintenance.box.addWidget(self.maintenance_verify_note)
        self.verify_source = QComboBox()
        self.verify_source.addItem("全部永久曲库", "all")
        self.verify_source.addItem("仅 PyPyDance", "pypy")
        self.verify_source.addItem("仅 WannaDance", "wd")
        self.verify_source.addItem("仅 DuDu FitDance", "dudu")
        self.verify_button = button("校验曲库", lambda: self.start_maintenance("verify"))
        self.verify_export_button = button("导出校验报告", self.export_verification_report)
        self.verify_export_button.setEnabled(False)
        self.maintenance_verify_row, _ = row(label("校验来源"), self.verify_source,
                                             self.verify_button, self.verify_export_button, stretch=True)
        maintenance.box.addWidget(self.maintenance_verify_row)
        self.cancel_maintenance_button = button("取消任务", self.cancel_maintenance)
        self.cancel_maintenance_button.setEnabled(False)
        self.maintenance_cancel_row, _ = row(self.cancel_maintenance_button, stretch=True)
        maintenance.box.addWidget(self.maintenance_cancel_row)
        self.maintenance_progress = QProgressBar()
        self.maintenance_progress.setTextVisible(True)
        self.maintenance_progress.hide()
        maintenance.box.addWidget(self.maintenance_progress)
        self.maintenance_status = label("尚未运行维护任务", muted=True, wrap=True)
        maintenance.box.addWidget(self.maintenance_status)
        box.addWidget(maintenance)
        box.addStretch()
        return self.with_apply_footer(area, "storage")
