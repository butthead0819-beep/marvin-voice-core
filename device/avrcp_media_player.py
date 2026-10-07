"""
avrcp_media_player.py — 車puck AVRCP MediaPlayer1 橋接（BlueZ Media1.RegisterPlayer）。

背景：2026-08-17 用 Soundcore 對照測試 BMW 頭單元每 30 秒規律斷 A2DP（見
project_car_puck_mk2_pi_zero2w_bt_mixer_validated 記憶）——Soundcore 完全沒斷，
BMW 會斷，兩者已知差異是 BMW 車機螢幕會顯示曲名/縮圖（主動查 AVRCP metadata），
puck_mixer 目前純 A2DP 裸串流、沒有任何 MediaPlayer1 物件可查，疑似車機查不到
metadata 判斷連線異常而斷開重連。這裡補上最小可用的 AVRCP Target metadata：報
Title/Artist/Album/PlaybackStatus。封面圖不在這裡——那要另外接 obexd 的 BIP
（Basic Imaging Profile）/OBEX API，是完全不同的一套機制，先評估要不要做。

2026-08-21：實測 D-Bus 層 metadata 已正確送達（`busctl` 直接查 BlueZ 持有的
player 物件，Title/Artist 都對），但 BMW 車機螢幕沒顯示；同一台車機接 iPhone
時能正常顯示演出者/專輯。已知差異：iPhone 的 player 一定回報完整控制能力，這裡
原本 `CanControl` 全 False（比照 BlueZ 官方 test/example-player 的 metadata-only
玩家設定）。懷疑車機把「不可控制」的來源當成不值得查 metadata 的裝置、跳過
AVRCP GetElementAttributes 查詢。改成回報 CanPlay/CanPause/CanGoNext/
CanGoPrevious/CanControl 全 True，Play/Pause/Next/Previous/Stop 這幾支 D-Bus
method 先做成安全的 no-op（車機真的按下去不會炸，但沒有實際效果）——真正的播放
控制走 Marvin 既有的語音/文字指令，這裡只是為了讓車機的能力協商判定「這是個
正常玩家、值得顯示資訊」。

2026-10-07：補 mpris:length（總長度）+ Position（播放位置）給 BlueZ，讓車機
能算「剩餘時間」——之前只報 Title/Artist/Album，車機沒有總長度/進度可用。
Pi 端有 ~5s 的 /audio_stream 預載緩衝，這裡送出的進度是 Mac 端 mixer 實際
播放的進度，車上實際聽到的聲音會比這個進度晚個幾秒——沒有做任何補償，顯示的
剩餘時間會比實際聽到的快一點。

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


def build_track_update(title, artist=None, album=None, duration_s=None, position_s=None) -> dict:
    """純函式（不碰 dbus）：組出 set_track() 要送進 BlueZ 的 metadata + 播放位置。
    mpris:length 只有 duration_s 有值且 >0 時才放進去——BlueZ/車機沒收到這個
    key 就當作「沒有總長度」，不要塞 0 進去誤導車機算出負的剩餘時間。"""
    metadata = {
        "xesam:title": title or "",
        "xesam:artist": [artist] if artist else [],
        "xesam:album": album or "",
    }
    if duration_s is not None and duration_s > 0:
        metadata["mpris:length"] = int(round(duration_s * 1_000_000))
    position_us = int(round(position_s * 1_000_000)) if position_s is not None else 0
    return {"metadata": metadata, "position_us": position_us}


if _AVAILABLE:

    class _Player(dbus.service.Object):
        """MPRIS 形狀的物件，供 BlueZ Media1.RegisterPlayer 轉發成 AVRCP metadata。
        2026-08-21：Can* 系列全部回 True——車機（BMW）疑似把「不可控制」的來源
        當成不值得顯示 metadata 的裝置，見本檔開頭說明。Play/Pause/Next/Previous/
        Stop 這幾支 method 因此也要存在（車機能力協商後真的可能呼叫），但只是
        no-op：真正的播放控制走 Marvin 既有的語音/文字指令。"""

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
                "CanGoNext": dbus.Boolean(True),
                "CanGoPrevious": dbus.Boolean(True),
                "CanPlay": dbus.Boolean(True),
                "CanPause": dbus.Boolean(True),
                "CanSeek": dbus.Boolean(False),
                "CanControl": dbus.Boolean(True),
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

        # 車機遙控 no-op：CanControl=True 後車機可能真的呼叫這幾支，接住但不做
        # 任何事（不丟例外、不改 PlaybackStatus）——播放控制走 Marvin 語音/文字。
        @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="")
        def Play(self):
            pass

        @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="")
        def Pause(self):
            pass

        @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="")
        def PlayPause(self):
            pass

        @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="")
        def Stop(self):
            pass

        @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="")
        def Next(self):
            pass

        @dbus.service.method(PLAYER_IFACE, in_signature="", out_signature="")
        def Previous(self):
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

    def set_track(self, title: str, artist: str = None, album: str = None,
                   duration_s: float = None, position_s: float = None):
        if not self.available:
            return
        update = build_track_update(title, artist, album, duration_s, position_s)
        raw_metadata = dict(update["metadata"])
        raw_metadata["xesam:artist"] = dbus.Array(raw_metadata["xesam:artist"], signature="s")
        if "mpris:length" in raw_metadata:
            raw_metadata["mpris:length"] = dbus.Int64(raw_metadata["mpris:length"])
        metadata = dbus.Dictionary(raw_metadata, signature="sv")
        GLib.idle_add(self._player.update, {
            "Metadata": metadata,
            "PlaybackStatus": "Playing",
            "Position": dbus.Int64(update["position_us"]),
        })

    def set_playing(self, playing: bool):
        if not self.available:
            return
        GLib.idle_add(self._player.update, {"PlaybackStatus": "Playing" if playing else "Stopped"})
