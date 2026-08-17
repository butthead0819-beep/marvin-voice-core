"""
avrcp_media_player.py — 車puck AVRCP MediaPlayer1 橋接（BlueZ Media1.RegisterPlayer）。

背景：2026-08-17 用 Soundcore 對照測試 BMW 頭單元每 30 秒規律斷 A2DP（見
project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶）——Soundcore 完全沒斷，
BMW 會斷，兩者已知差異是 BMW 車機螢幕會顯示曲名/縮圖（主動查 AVRCP metadata），
puck_mixer 目前純 A2DP 裸串流、沒有任何 MediaPlayer1 物件可查，疑似車機查不到
metadata 判斷連線異常而斷開重連。這裡補上最小可用的 AVRCP Target metadata：只報
Title/Artist/PlaybackStatus，不接受車機遙控（CanControl 全 False，比照 BlueZ 官方
test/example-player 的 metadata-only 玩家設定）。封面圖不在這裡——那要另外接
obexd 的 BIP（Basic Imaging Profile）/OBEX API，是完全不同的一套機制，等這支先在
車上驗證真的解決斷線問題再評估要不要做。

需要系統套件（這台 Pi 的 volume_server.py 跑系統 python3、沒用 venv，直接
apt 裝，不要 pip）：
    sudo apt-get install -y python3-dbus python3-gi
沒裝就整個 no-op（import 失敗，跟 device/volume_server.py 對 PuckMicAecLoop 的
容錯方式一致）——不影響音樂播放本身，只是車機螢幕沒曲名可顯示。
"""
import threading

try:
    import dbus
    import dbus.mainloop.glib
    import dbus.service
    from gi.repository import GLib
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
PLAYER_PATH = "/marvin/player0"


if _AVAILABLE:

    class _Player(dbus.service.Object):
        """MPRIS 形狀的物件，供 BlueZ Media1.RegisterPlayer 轉發成 AVRCP metadata。
        CanControl 系列全 False：只報資訊、不接受車機遙控（比照 BlueZ 官方
        test/example-player 的預設玩家設定）。"""

        def __init__(self, bus):
            dbus.service.Object.__init__(self, bus, PLAYER_PATH)
            self._lock = threading.Lock()
            self.properties = dbus.Dictionary({
                "PlaybackStatus": "Stopped",
                "Identity": "Marvin",
                "LoopStatus": "None",
                "Rate": dbus.Double(1.0),
                "Shuffle": dbus.Boolean(False),
                "Metadata": dbus.Dictionary({}, signature="sv"),
                "Volume": dbus.Double(1.0),
                "Position": dbus.Int64(0),
                "MinimumRate": dbus.Double(1.0),
                "MaximumRate": dbus.Double(1.0),
                "CanGoNext": dbus.Boolean(False),
                "CanGoPrevious": dbus.Boolean(False),
                "CanPlay": dbus.Boolean(False),
                "CanPause": dbus.Boolean(False),
                "CanSeek": dbus.Boolean(False),
                "CanControl": dbus.Boolean(False),
            }, signature="sv")

        def get_path(self):
            return dbus.ObjectPath(PLAYER_PATH)

        @dbus.service.method(PROPS_IFACE, in_signature="ssv", out_signature="")
        def Set(self, interface, key, value):
            with self._lock:
                self.properties[key] = value

        @dbus.service.method(PROPS_IFACE, in_signature="ss", out_signature="v")
        def Get(self, interface, key):
            with self._lock:
                return self.properties[key]

        @dbus.service.method(PROPS_IFACE, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):
            with self._lock:
                return dbus.Dictionary(self.properties, signature="sv")

        @dbus.service.signal(PROPS_IFACE, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):
            pass

        def update(self, changed: dict):
            """執行緒安全地更新屬性 + 廣播 PropertiesChanged。呼叫端（另一條
            thread）不用管 GLib mainloop，透過 AvrcpMediaPlayer.set_track()
            用 GLib.idle_add 排進這個物件所在的 mainloop thread 再執行。"""
            with self._lock:
                self.properties.update(changed)
            self.PropertiesChanged(
                PLAYER_IFACE,
                dbus.Dictionary(changed, signature="sv"),
                dbus.Array([], signature="s"),
            )


class AvrcpMediaPlayer:
    """puck_mixer 換歌時呼叫 set_track()/set_playing()，這裡負責轉成 BlueZ
    MediaPlayer1（透過 Media1.RegisterPlayer）讓 AVRCP 對端（車機）查得到。
    裝置沒裝 python3-dbus/python3-gi、或跟 BlueZ 註冊失敗時整個 no-op
    （available 屬性看得到，set_track()/set_playing() 靜默略過）。"""

    def __init__(self, adapter_path: str = "/org/bluez/hci0"):
        self.available = False
        self._adapter_path = adapter_path
        self._player = None
        self._loop = None
        if not _AVAILABLE:
            return
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        try:
            bus = dbus.SystemBus()
            self._player = _Player(bus)
            media = dbus.Interface(
                bus.get_object("org.bluez", self._adapter_path), "org.bluez.Media1"
            )
            media.RegisterPlayer(self._player.get_path(), dbus.Dictionary({}, signature="sv"))
            self.available = True
            print(f"🎵 [AvrcpMediaPlayer] 已註冊 {PLAYER_PATH} → {self._adapter_path}", flush=True)
        except Exception as e:
            print(f"⚠️ [AvrcpMediaPlayer] 註冊失敗，AVRCP metadata 停用: {e}", flush=True)
            return
        self._loop = GLib.MainLoop()
        self._loop.run()

    def set_track(self, title: str, artist: str = None):
        if not self.available:
            return
        metadata = dbus.Dictionary({
            "xesam:title": title or "",
            "xesam:artist": dbus.Array([artist] if artist else [], signature="s"),
        }, signature="sv")
        GLib.idle_add(self._player.update, {"Metadata": metadata, "PlaybackStatus": "Playing"})

    def set_playing(self, playing: bool):
        if not self.available:
            return
        GLib.idle_add(self._player.update, {"PlaybackStatus": "Playing" if playing else "Stopped"})
