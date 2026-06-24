"""B 骨架 — 不對等漫畫拼版測試（先紅後綠）。

核心需求：
- 日本漫畫不對等切割（格子大小不均）
- 格子大小 = 該時段熱度（熱的格子大）
- 馬文碎念用 CJK 字型疊字
"""
from PIL import Image

from diary_comic.layout import (
    pick_template,
    assign_boxes,
    compose_page,
    cover_fit,
    plan_boxes,
    nearest_aspect_ratio,
    wrap_text,
    _load_font,
    Panel,
)


def test_wrap_text_every_line_fits_within_width():
    font = _load_font(28)
    max_w = 300
    text = "從泡麵聊到擴大機，你們的生活還真有聲有色啊，這群人到底在忙什麼東西呢"
    for ln in wrap_text(text, font, max_w):
        assert font.getlength(ln) <= max_w + 1  # 容浮點誤差


def test_wrap_text_preserves_all_characters():
    font = _load_font(28)
    text = "討論數位擴大機規格、無線麥克風配件及舊款山水音響的汰換"
    assert "".join(wrap_text(text, font, 240)) == text  # 不丟字


def test_wrap_text_short_text_single_line():
    font = _load_font(28)
    assert wrap_text("嘆。", font, 500) == ["嘆。"]


def test_wrap_text_never_empty_loops_on_too_narrow():
    # 寬度比單字還窄 → 每行至少一字，不無限迴圈
    font = _load_font(28)
    lines = wrap_text("泡麵擴大機", font, 1)
    assert "".join(lines) == "泡麵擴大機"


def test_cover_fit_returns_exact_target_size():
    for src, tgt in [((100, 50), (50, 50)), ((100, 100), (50, 100)), ((40, 90), (80, 80))]:
        out = cover_fit(Image.new("RGB", src), tgt[0], tgt[1])
        assert out.size == tgt


def test_cover_fit_does_not_distort_uses_max_scale_crop():
    # 寬來源塞進方框：應放大到填滿後裁切，不是壓扁。
    # 200x100 → 50x50：scale=max(50/200,50/100)=0.5 → 100x50 → 置中裁 50x50
    src = Image.new("RGB", (200, 100))
    out = cover_fit(src, 50, 50)
    assert out.size == (50, 50)


def _mean_lum(im):
    from PIL import ImageStat
    return ImageStat.Stat(im.convert("L")).mean[0]


def test_cover_fit_focus_y_biases_vertical_crop():
    """focus_y 控制保留哪一段；預設 0.5＝置中（向後相容）。
    直幅塞寬扁框時，對準主體那段才不會被切掉（Hero 斜切臉被切的修法）。"""
    from PIL import ImageDraw
    src = Image.new("RGB", (100, 300), (0, 0, 0))
    ImageDraw.Draw(src).rectangle([0, 0, 100, 100], fill=(255, 255, 255))  # 上 1/3 是主體
    top = cover_fit(src, 100, 100, focus_y=0.17)     # 對準上 1/3
    bottom = cover_fit(src, 100, 100, focus_y=0.83)  # 對準下 1/3
    assert _mean_lum(top) > _mean_lum(bottom)        # 對準主體那張保住白色主體


def test_hero_upper_focus_is_biased_above_center():
    """Hero 斜切上梯形的裁切焦點偏上（保住角色的臉），不是置中。"""
    from diary_comic.layout import _HERO_UPPER_FOCUS_Y
    assert 0.2 <= _HERO_UPPER_FOCUS_Y < 0.5


def test_nearest_aspect_ratio_tall_box_is_portrait():
    page = (1200, 1600)
    tall_box = (0.0, 0.0, 0.30, 0.90)  # 窄高
    name = nearest_aspect_ratio(tall_box, page)
    w, h = (int(x) for x in name.split(":"))
    assert w < h  # 直幅


def test_nearest_aspect_ratio_wide_box_is_landscape():
    page = (1200, 1600)
    wide_box = (0.0, 0.0, 1.0, 0.18)  # 寬扁
    name = nearest_aspect_ratio(wide_box, page)
    w, h = (int(x) for x in name.split(":"))
    assert w > h  # 橫幅


def test_plan_boxes_returns_one_box_per_panel_largest_to_hottest():
    heats = [1, 9, 2]
    boxes = plan_boxes(heats)
    assert len(boxes) == 3
    areas = [w * h for (_x, _y, w, h) in boxes]
    assert areas[1] == max(areas)  # 最熱拿最大格


def test_pick_template_returns_n_boxes():
    boxes = pick_template(6)
    assert len(boxes) == 6


def test_template_is_asymmetric_not_uniform_grid():
    # 不對等切割：格子面積必須有差異，不能全部一樣大
    boxes = pick_template(6)
    areas = {round(w * h, 4) for (_x, _y, w, h) in boxes}
    assert len(areas) > 1, "格子面積全相同 = 均等方格，不是不對等切割"


def test_template_boxes_stay_within_unit_canvas():
    for n in (3, 4, 5, 6):
        for (x, y, w, h) in pick_template(n):
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
            assert x + w <= 1.0001 and y + h <= 1.0001


def test_assign_boxes_gives_largest_box_to_highest_heat():
    boxes = pick_template(4)
    heats = [1, 9, 2, 3]  # index 1 最熱
    order = assign_boxes(heats, boxes)  # 回傳 panel_index -> box_index
    # 最熱那格拿到面積最大的 box
    areas = [w * h for (_x, _y, w, h) in boxes]
    largest_box = max(range(len(boxes)), key=lambda i: areas[i])
    assert order[1] == largest_box


def test_compose_page_returns_image_of_requested_size():
    panels = [
        Panel(image=Image.new("RGB", (50, 50), (200, 150, 100)),
              heat=h, caption=f"馬文碎念 {h}")
        for h in (5, 2, 8)
    ]
    page = compose_page(panels, page_size=(1200, 1600))
    assert isinstance(page, Image.Image)
    assert page.size == (1200, 1600)


def test_compose_page_handles_cjk_caption_without_crashing():
    panels = [Panel(image=Image.new("RGB", (50, 50), (10, 10, 10)),
                    heat=3, caption="從藝術墜落到本能，速度快得令人心碎。")]
    page = compose_page(panels, page_size=(600, 800))
    assert page.size == (600, 800)


# ---- 斜格（傾斜分鏡）----
from diary_comic.layout import slanted_bands


def test_slanted_bands_one_quad_per_panel():
    polys = slanted_bands([5, 8, 3])
    assert len(polys) == 3
    assert all(len(p) == 4 for p in polys)  # 每格四邊形


def test_slanted_bands_height_proportional_to_heat():
    polys = slanted_bands([2, 10, 2])  # 中間最熱
    def bbox_h(p):
        ys = [y for _x, y in p]
        return max(ys) - min(ys)
    hs = [bbox_h(p) for p in polys]
    assert hs[1] == max(hs)  # 最熱格最高


def test_slanted_bands_top_and_bottom_edges_stay_flat():
    polys = slanted_bands([4, 4, 4])
    # 第一格上緣 y=0、最後一格下緣 y=1（頁面上下不歪）
    assert all(abs(y) < 1e-6 for _x, y in polys[0][:2])
    assert all(abs(y - 1.0) < 1e-6 for _x, y in polys[-1][2:])


def test_slanted_bands_interior_divider_is_slanted():
    polys = slanted_bands([4, 4], tilt=0.04)
    # 兩格之間的分隔線：左右兩端 y 不同 = 傾斜
    bottom_left_y = polys[0][3][1]
    bottom_right_y = polys[0][2][1]
    assert abs(bottom_left_y - bottom_right_y) > 1e-3


def test_slanted_bands_adjacent_share_divider_edge():
    polys = slanted_bands([4, 4, 4])
    # 第 0 格下緣兩點 == 第 1 格上緣兩點（共邊、不重疊不留縫）
    assert polys[0][3] == polys[1][0]  # bottom-left == top-left
    assert polys[0][2] == polys[1][1]  # bottom-right == top-right


def test_slanted_bands_stay_in_unit_canvas():
    for p in slanted_bands([3, 7, 2, 5]):
        for x, y in p:
            assert 0.0 <= x <= 1.0 and -1e-6 <= y <= 1.0 + 1e-6


def test_compose_page_slanted_returns_requested_size():
    from diary_comic.layout import compose_page_slanted
    panels = [Panel(image=Image.new("RGB", (60, 60), (180, 140, 100)),
                    heat=h, caption=f"斜格字幕測試 {h} 號") for h in (4, 9, 3)]
    page = compose_page_slanted(panels, page_size=(1080, 1920))
    assert isinstance(page, Image.Image) and page.size == (1080, 1920)


# ---- Hero 斜切：一個矩形內用對角線切成上下兩個梯形 ----
from diary_comic.layout import hero_split_polys


def test_hero_split_returns_upper_and_lower_quads():
    up, lo = hero_split_polys(0, 0, 100, 200, tilt=0.12)
    assert len(up) == 4 and len(lo) == 4


def test_hero_split_upper_touches_flat_top():
    up, _lo = hero_split_polys(0, 0, 100, 200, tilt=0.12)
    # 上格的上緣兩點 y=0（平的，字幕可貼上緣）
    assert up[0][1] == 0 and up[1][1] == 0


def test_hero_split_lower_touches_flat_bottom():
    _up, lo = hero_split_polys(0, 0, 100, 200, tilt=0.12)
    # 下格的下緣兩點 y=200（平的，字幕可貼下緣）
    assert lo[2][1] == 200 and lo[3][1] == 200


def test_hero_split_shares_diagonal_edge():
    up, lo = hero_split_polys(0, 0, 100, 200, tilt=0.12)
    assert up[3] == lo[0] and up[2] == lo[1]  # 共用對角線


def test_hero_split_diagonal_is_slanted():
    up, _lo = hero_split_polys(0, 0, 100, 200, tilt=0.12)
    # 對角線左右端 y 不同 = 斜的
    assert up[3][1] != up[2][1]


def test_compose_page_hero_handles_single_and_duo_rows():
    from diary_comic.layout import compose_page_hero
    def P(h, c):
        return Panel(image=Image.new("RGB", (60, 60), (150, 140, 120)), heat=h, caption=c)
    rows = [("single", P(4, "單格 A")),
            ("duo", P(9, "上格 Hero 金句"), P(8, "下格夥伴")),
            ("single", P(3, "單格 B"))]
    page = compose_page_hero(rows, (1080, 1920))
    assert isinstance(page, Image.Image) and page.size == (1080, 1920)


def test_compose_page_webtoon_is_tall_strip():
    from diary_comic.layout import compose_page_webtoon
    def P(h, c):
        return Panel(image=Image.new("RGB", (80, 80), (160, 150, 130)), heat=h, caption=c)
    page = compose_page_webtoon([P(4, "格一"), P(9, "格二"), P(3, "格三")], page_width=1080)
    assert page.width == 1080
    assert page.height > 1080  # 長條，比寬高很多


# ---- 變動 gutter：同主題窄、跳主題寬 ----
def test_gutter_between_narrow_for_similar_wide_for_different():
    from diary_comic.layout import gutter_between
    g_sim = gutter_between("討論喇叭的阻抗規格", "討論喇叭的阻抗規格細節", base=60)
    g_diff = gutter_between("討論喇叭", "聊PS4遊戲跟泡麵", base=60)
    assert g_sim < g_diff  # 相似主題 gutter 窄、跳主題寬


def test_gutter_between_stays_positive():
    from diary_comic.layout import gutter_between
    assert gutter_between("a", "b", base=60) > 0


# ---- Inset 反應特寫：大格上疊小格 ----
def test_paste_inset_keeps_page_size():
    from diary_comic.layout import paste_inset
    page = Image.new("RGB", (400, 400), (250, 250, 250))
    inset = Image.new("RGB", (80, 80), (10, 10, 10))
    out = paste_inset(page, inset, 50, 50, 120, 120)
    assert out.size == (400, 400)


def test_panel_accepts_inset_field():
    p = Panel(image=Image.new("RGB", (60, 60)), heat=5, caption="x",
              inset=Image.new("RGB", (30, 30)))
    assert p.inset is not None


def test_compose_webtoon_renders_panel_with_inset():
    from diary_comic.layout import compose_page_webtoon
    panels = [Panel(image=Image.new("RGB", (80, 80)), heat=6, caption="笑點",
                    inset=Image.new("RGB", (40, 40), (200, 50, 50)))]
    page = compose_page_webtoon(panels, page_width=1080)
    assert page.width == 1080  # 有 inset 也不爆


# ---- 一格 meme ----
def test_compose_meme_returns_size_with_top_and_bottom():
    from diary_comic.layout import compose_meme
    img = Image.new("RGB", (200, 200), (180, 150, 120))
    page = compose_meme(img, top="他把球踢進自家球門", bottom="全場笑爛", size=(1080, 1080))
    assert isinstance(page, Image.Image) and page.size == (1080, 1080)


def test_compose_meme_solo_no_bottom():
    from diary_comic.layout import compose_meme
    img = Image.new("RGB", (200, 200))
    page = compose_meme(img, top="梗自己講", bottom="", size=(800, 800))
    assert page.size == (800, 800)  # 無 Marvin（單飛）也不爆


# ---- 大砸框：一頁一個高潮格 ≥40%，其餘鋪陳 ----
def test_splash_layout_climax_is_at_least_40_percent():
    from diary_comic.layout import splash_layout
    W, H = 1080, 1920
    _support, climax = splash_layout(3, (W, H), climax_frac=0.45)
    cx0, cy0, cx1, cy1 = climax
    area_frac = ((cx1 - cx0) * (cy1 - cy0)) / (W * H)
    assert area_frac >= 0.40  # 高潮格佔全頁 ≥40%


def test_splash_layout_one_support_box_per_panel():
    from diary_comic.layout import splash_layout
    support, _climax = splash_layout(3, (1080, 1920))
    assert len(support) == 3  # 鋪陳格數對


def test_compose_splash_page_returns_size():
    from diary_comic.layout import compose_splash_page
    support = [Panel(image=Image.new("RGB", (60, 60)), heat=3, caption=f"鋪陳{i}") for i in range(3)]
    climax = Panel(image=Image.new("RGB", (80, 80), (200, 50, 50)), heat=10, caption="爆笑高潮")
    page = compose_splash_page(support, climax, page_size=(1080, 1920))
    assert isinstance(page, Image.Image) and page.size == (1080, 1920)


# ---- 鐵律：垂直格線窄、水平格線寬（防跳行）----
def test_splash_gutters_vertical_narrow_horizontal_wide():
    from diary_comic.layout import splash_layout
    support, climax = splash_layout(2, (1080, 1920))  # 2 格並排一列
    v_gap = support[1][0] - support[0][2]   # 兩欄之間（垂直格線）
    h_gap = climax[1] - support[0][3]       # 鋪陳列底 ↔ 高潮頂（水平格線）
    assert v_gap > 0 and h_gap > v_gap      # 水平格線寬 > 垂直格線窄


# ---- 同源推鏡：一張高清素材裁多格（省 API、零飄移）----
def test_crops_from_source_one_panel_per_spec():
    from diary_comic.layout import crops_from_source, CropSpec
    src = Image.new("RGB", (800, 600))
    panels = crops_from_source(src, [CropSpec((0, 0, 1, 1), "遠"), CropSpec((0.2, 0.2, 0.8, 0.8), "中")])
    assert len(panels) == 2 and panels[0].caption == "遠"


def test_crops_from_source_wide_is_full_image():
    from diary_comic.layout import crops_from_source, CropSpec
    src = Image.new("RGB", (800, 600))
    p = crops_from_source(src, [CropSpec((0, 0, 1, 1))])[0]
    assert p.image.size == (800, 600)


def test_crops_from_source_tighter_crop_has_fewer_pixels():
    from diary_comic.layout import crops_from_source, CropSpec
    src = Image.new("RGB", (800, 600))
    wide, close = crops_from_source(src, [CropSpec((0, 0, 1, 1)), CropSpec((0.35, 0.35, 0.65, 0.65))])
    assert close.image.size[0] < wide.image.size[0]  # 特寫像素更少（裁得緊）


def test_crops_from_source_carries_heat():
    from diary_comic.layout import crops_from_source, CropSpec
    src = Image.new("RGB", (800, 600))
    p = crops_from_source(src, [CropSpec((0, 0, 1, 1), heat=9)])[0]
    assert p.heat == 9


def test_pushin_specs_three_progressively_tighter():
    from diary_comic.layout import pushin_specs
    specs = pushin_specs()
    assert len(specs) == 3  # 遠→中→特
    areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in (s.box for s in specs)]
    assert areas[0] > areas[1] > areas[2]  # 一路推緊


# ---- pair row：遠景同源切左右不對等兩格 ----
def test_compose_page_hero_pair_row_returns_size():
    from diary_comic.layout import compose_page_hero
    left = Panel(image=Image.new("RGB", (60, 60)), heat=3, caption="左")
    right = Panel(image=Image.new("RGB", (60, 60)), heat=4, caption="右")
    mid = Panel(image=Image.new("RGB", (60, 60)), heat=5, caption="中景")
    rows = [("pair", left, right, 0.30), ("single", mid)]
    page = compose_page_hero(rows, (1080, 1920))
    assert isinstance(page, Image.Image) and page.size == (1080, 1920)


# ---- 遠景精準對切左右（零重疊、兩主體）----
def test_split_lr_specs_exact_partition_no_overlap():
    from diary_comic.layout import split_lr_specs
    left, right = split_lr_specs(0.30)
    assert left.box[2] == right.box[0]          # 邊界共用：左右剛好相接
    assert left.box[0] == 0.0 and right.box[2] == 1.0  # 涵蓋全寬、不重疊
    assert abs((left.box[2] - left.box[0]) - 0.30) < 1e-9  # 左 30%


def test_split_lr_no_duplicate_content():
    from diary_comic.layout import split_lr_specs, crops_from_source
    src = Image.new("RGB", (1000, 600))
    left, right = split_lr_specs(0.30)
    lp, rp = crops_from_source(src, [left, right])
    assert lp.image.size[0] + rp.image.size[0] == 1000  # 兩格寬相加=原寬，無重疊


# ---- 格1 焦點+全景（B 打法）----
def test_zoom_wide_specs_left_zoom_right_full():
    from diary_comic.layout import zoom_wide_specs
    zoom, full = zoom_wide_specs(focus_box=(0.34, 0.02, 0.66, 0.52))
    assert full.box == (0.0, 0.0, 1.0, 1.0)              # 右=全景
    assert zoom.box == (0.34, 0.02, 0.66, 0.52)          # 左=焦點放大
    za = (zoom.box[2] - zoom.box[0]) * (zoom.box[3] - zoom.box[1])
    assert za < 1.0                                       # 焦點是局部、不是全圖


def test_zoom_wide_specs_carries_captions():
    from diary_comic.layout import zoom_wide_specs
    z, w = zoom_wide_specs((0.3, 0.0, 0.6, 0.5), captions=["講者", "全場"])
    assert z.caption == "講者" and w.caption == "全場"


# ---- vpair（垂直兩格）/ quad（2x2 四宮格）row 型別 ----
def test_compose_page_hero_vpair_and_quad_rows():
    from diary_comic.layout import compose_page_hero
    p = lambda h: Panel(image=Image.new("RGB", (60, 60)), heat=h)
    rows = [("vpair", p(3), p(3), 0.45),
            ("quad", p(3), p(3), p(3), p(3)),
            ("duo", p(9), p(11))]
    page = compose_page_hero(rows, (1080, 1920))
    assert isinstance(page, Image.Image) and page.size == (1080, 1920)
