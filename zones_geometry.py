"""桌面分区的几何常量。

屏幕 2560x1440、显示缩放 100%（AppliedDPI=96），CSS px = 物理 px；
任务栏占底部 48px，可用高度到 y=1392。
行步长 98 与列步长 150 由实际图标坐标反推（zones_geometry.observed_steps
可在实机复核）。注意 LVM_GETITEMSPACING 的返回值与实际布局不符，不可用；
且步长随最长标签宽度变化，因此这两个值是设计选择而非系统保证。
"""

SCREEN_W = 2560
SCREEN_H = 1440
WORK_H = 1392

ROW_STEP = 98
# 列步长随最长标签宽度变化，是设计选择而非系统值；150 为 2026-09-23 本机实测
# （图标视图三列坐标 13/163/313 的间隔），足以容纳当前最长标签。
COL_STEP = 150

# 右栏避让线：屏宽减去 AGENT DECK 右栏 36rem(=576px)。任何图标格不得越过。
AVOID_X = SCREEN_W - 576

# 首行必须让开 DECK 顶栏：#deck 有 2.2rem 上边距，顶栏 4.6rem + 1px 底线，
# 横线实际在 y≈109；y=100 的晶格行会压线（2026-09-24 实机截图确认），
# 故取再下一行 198。
APP_ORIGIN = (13, 198)
APP_COLS = 6
APP_ROWS = 2
APP_SLOTS = APP_COLS * APP_ROWS

# 标签独占应用区与文档区之间那个无图标的空行
LABEL_Y = 420

DOC_ORIGIN = (13, 492)
DOC_MAX_ROWS = 8

# 必须落在晶格上（y ≡ APP_ORIGIN[1] mod ROW_STEP），否则会被 explorer 吸附走；
# 1276 是满足格底 ≤ WORK_H 的最底晶格行
RECYCLE_POS = (13, 1276)


def observed_steps(coords):
    """从一组图标坐标反推 (列步长, 行步长)；某轴不足两个不同值时该轴为 None。"""
    xs = sorted({x for x, _ in coords})
    ys = sorted({y for _, y in coords})
    col = min((b - a for a, b in zip(xs, xs[1:])), default=None)
    row = min((b - a for a, b in zip(ys, ys[1:])), default=None)
    return (col, row)


def app_slot(col, row):
    """应用区第 col 列、第 row 行（均从 0 起）的左上角坐标。行优先由调用方换算。"""
    return (APP_ORIGIN[0] + col * COL_STEP, APP_ORIGIN[1] + row * ROW_STEP)


def doc_cell(col, row):
    return (DOC_ORIGIN[0] + col * COL_STEP, DOC_ORIGIN[1] + row * ROW_STEP)
