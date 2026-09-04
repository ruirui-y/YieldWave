"""YieldWave 全局深色主题。

集中管理所有颜色，禁止在 widget 里散落 #xxxxxx。
所有页面通过 apply_dark_theme(app) 一次性注入 QSS，单个 widget 不再写大段 stylesheet。

设计原则：
- 深灰黑背景（非纯黑 #000000），不刺眼；
- 红绿仅用于真正的交易动作（BUY / SELL），其余状态用蓝/灰/黄；
- 信息层级明显，避免大面积荧光色。
"""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

# ========= 颜色常量（任何 widget 都只允许引用这里） =========
APP_BG = "#0F1115"        # 应用主背景
CARD_BG = "#171A21"       # 卡片背景
CARD_BG_ALT = "#1D212A"   # 卡片次级背景（tooltip / hover）
BORDER = "#2A303B"       # 通用边框

TEXT_PRIMARY = "#F3F4F6"     # 主文字
TEXT_SECONDARY = "#9CA3AF"  # 次文字
TEXT_MUTED = "#6B7280"       # 弱文字

BUY = "#22C55E"     # 买入绿
SELL = "#EF4444"    # 卖出红
HOLD = "#3B82F6"    # 持有蓝
WAIT = "#9CA3AF"    # 等待灰
WARNING = "#F59E0B"  # 警告黄

DP2_LINE = "#38BDF8"   # 走势图 D/P2 主线（青蓝）
M42_LINE = "#A3A3A3"  # 走势图 M42 中枢（灰）

# 买卖线一致配色（与 strategy 内 key 对齐）
THRESHOLD_COLORS = {
    "A_buy": "#22C55E", "A_sell": "#EF4444",
    "B_buy": "#10B981", "B_sell": "#F97316",
    "C_buy": "#06B6D4", "C_sell": "#EC4899",
}


def build_qss() -> str:
    """生成全局 QSS 字符串。所有窗口共享同一份。"""
    return f"""
    QWidget {{
        background-color: {APP_BG};
        color: {TEXT_PRIMARY};
        font-family: "Microsoft YaHei", "PingFang SC", "SimHei", "DejaVu Sans";
        font-size: 13px;
    }}
    QMainWindow, QDialog {{
        background-color: {APP_BG};
    }}
    QFrame#HeroBanner {{
        background-color: {CARD_BG};
        border: 1px solid {BORDER};
        border-left: 4px solid {WAIT};
        border-radius: 8px;
    }}
    QFrame#HeroBanner[action="buy"]  {{ border-left-color: {BUY}; }}
    QFrame#HeroBanner[action="sell"] {{ border-left-color: {SELL}; }}
    QFrame#HeroBanner[action="wait"] {{ border-left-color: {WAIT}; }}
    QLabel {{
        background: transparent;
        color: {TEXT_PRIMARY};
    }}
    QLabel#KpiLabel {{
        color: {TEXT_SECONDARY};
        font-size: 12px;
    }}
    QLabel#KpiValue {{
        color: {TEXT_PRIMARY};
        font-size: 16px;
        font-weight: 600;
    }}
    QLabel#KpiValueEstimate {{
        color: {WARNING};
        font-size: 16px;
        font-weight: 600;
    }}
    QLabel#KpiValueEmphasis {{
        color: {TEXT_PRIMARY};
        font-size: 18px;
        font-weight: 700;
    }}
    QLabel#HeroTitle {{ font-size: 16pt; font-weight: 700; color: {TEXT_PRIMARY}; }}
    QLabel#HeroSub {{ font-size: 10pt; color: {TEXT_SECONDARY}; }}
    QLabel#EstimateTag {{
        color: {WARNING};
        font-size: 11px;
        font-weight: 600;
        background-color: {CARD_BG_ALT};
        border: 1px solid {BORDER};
        border-radius: 3px;
        padding: 0 4px;
    }}
    QLabel#SectionTitle {{
        color: {TEXT_PRIMARY};
        font-size: 14px;
        font-weight: 700;
        background-color: {CARD_BG_ALT};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 4px 8px;
    }}
    QLabel#ActionBuy {{ color: {BUY}; font-size: 16px; font-weight: 700; padding: 2px; }}
    QLabel#ActionSell {{ color: {SELL}; font-size: 16px; font-weight: 700; padding: 2px; }}
    QLabel#ActionHold {{ color: {HOLD}; font-size: 16px; font-weight: 700; padding: 2px; }}
    QLabel#ActionWait {{ color: {WAIT}; font-size: 16px; font-weight: 700; padding: 2px; }}
    QLabel#WarnNote {{ color: {WARNING}; font-size: 11px; }}
    QLabel#MutedNote {{ color: {TEXT_MUTED}; font-size: 11px; }}

    QGroupBox {{
        background-color: {CARD_BG};
        border: 1px solid {BORDER};
        border-radius: 8px;
        margin-top: 12px;
        padding: 8px;
        color: {TEXT_PRIMARY};
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 8px;
        padding: 0 6px;
        color: {TEXT_SECONDARY};
    }}

    QTabWidget::pane {{
        border: 1px solid {BORDER};
        background: {APP_BG};
        top: -1px;
    }}
    QTabBar::tab {{
        background: {CARD_BG};
        color: {TEXT_SECONDARY};
        border: 1px solid {BORDER};
        border-bottom: none;
        padding: 6px 16px;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border-color: {BORDER};
    }}
    QTabBar::tab:hover:!selected {{
        background: {CARD_BG_ALT};
    }}

    QPushButton {{
        background-color: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{
        background-color: #252B36;
        border-color: #3A4250;
    }}
    QPushButton:pressed {{
        background-color: #1A1E26;
    }}
    QPushButton:disabled {{
        color: {TEXT_MUTED};
        background-color: {CARD_BG};
        border-color: {BORDER};
    }}
    QPushButton#PrimaryButton {{
        background-color: #1E3A5F;
        color: {TEXT_PRIMARY};
        border: 1px solid #2A5377;
    }}
    QPushButton#PrimaryButton:hover {{
        background-color: #234670;
    }}

    QTableWidget {{
        background-color: {CARD_BG};
        alternate-background-color: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        gridline-color: {BORDER};
        selection-background-color: #2A3548;
        selection-color: {TEXT_PRIMARY};
        outline: 0;
    }}
    QTableWidget::item {{
        padding: 4px 6px;
        border: 0;
    }}
    QTableWidget::item:selected {{
        background-color: #2A3548;
    }}
    QHeaderView::section {{
        background-color: {CARD_BG_ALT};
        color: {TEXT_SECONDARY};
        padding: 6px;
        border: none;
        border-right: 1px solid {BORDER};
        border-bottom: 1px solid {BORDER};
        font-weight: 600;
    }}
    QTableCornerButton::section {{
        background-color: {CARD_BG_ALT};
        border: none;
        border-bottom: 1px solid {BORDER};
        border-right: 1px solid {BORDER};
    }}

    QComboBox {{
        background-color: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 4px 8px;
        min-width: 80px;
    }}
    QComboBox:hover {{
        border-color: #3A4250;
    }}
    QComboBox::drop-down {{
        border: none;
        width: 20px;
    }}
    QComboBox::down-arrow {{
        image: none;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {TEXT_SECONDARY};
        width: 0; height: 0;
        margin-right: 6px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        selection-background-color: #2A3548;
        outline: 0;
    }}

    QLineEdit, QDoubleSpinBox, QSpinBox, QTextEdit {{
        background-color: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        border-radius: 4px;
        padding: 4px 6px;
        selection-background-color: #2A3548;
        selection-color: {TEXT_PRIMARY};
    }}
    QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QTextEdit:focus {{
        border: 1px solid #3A6EA5;
    }}
    QDoubleSpinBox::up-button, QSpinBox::up-button,
    QDoubleSpinBox::down-button, QSpinBox::down-button {{
        background-color: {CARD_BG};
        border: none;
        width: 16px;
    }}

    QScrollBar:vertical {{
        background: {CARD_BG};
        width: 12px;
        margin: 0;
    }}
    QScrollBar::handle:vertical {{
        background: #3A4250;
        border-radius: 4px;
        min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: #4A5260;
    }}
    QScrollBar:horizontal {{
        background: {CARD_BG};
        height: 12px;
        margin: 0;
    }}
    QScrollBar::handle:horizontal {{
        background: #3A4250;
        border-radius: 4px;
        min-width: 24px;
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        background: none;
        border: none;
        width: 0; height: 0;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: {CARD_BG};
    }}


    QLabel#AppTitle {{ font-size: 13pt; font-weight: 700; color: {TEXT_PRIMARY}; }}
    QFrame#HeroBanner {{
      background: {CARD_BG}; border: 1px solid {BORDER};
      border-left: 4px solid {WAIT}; border-radius: 6px;
    }}
    QFrame#HeroBanner[action="buy"]  {{ border-left-color: {BUY}; }}
    QFrame#HeroBanner[action="sell"] {{ border-left-color: {SELL}; }}
    QLabel#HeroTitle {{ font-size: 15pt; font-weight: 700; color: {TEXT_PRIMARY}; }}
    QLabel#HeroSub   {{ font-size: 10pt; color: {TEXT_SECONDARY}; }}
    QFrame#KpiStrip  {{ background: {CARD_BG}; border: 1px solid {BORDER};
                       border-radius: 8px; }}
    QFrame#KpiStrip QFrame[frameShape="4"] {{ color: {BORDER}; }}
    QLabel#KpiLabel  {{ font-size: 10pt; color: {TEXT_MUTED}; }}
    QLabel#KpiValue  {{ font-size: 17pt; font-weight: 600; color: {TEXT_PRIMARY}; }}
    QLabel#KpiValue[est="true"] {{ color: {WARNING}; }}
    QLabel#KpiSub    {{ font-size: 9pt;  color: {TEXT_SECONDARY}; }}
    QFrame#SideCard  {{ background: {CARD_BG}; border: 1px solid {BORDER};
                       border-radius: 8px; }}
    QLabel#SideCardTitle {{ font-size: 11pt; font-weight: 600;
                           color: {TEXT_PRIMARY}; }}
    QLabel#MutedNote {{ font-size: 9pt; color: {TEXT_MUTED}; }}
    QLabel#WarnNote  {{ font-size: 10pt; color: {WARNING}; }}
    QTableWidget#ThresholdTable {{ background: transparent; border: none;
                                  gridline-color: transparent; }}
    QTableWidget#ThresholdTable::item {{ border-bottom: 1px solid {CARD_BG_ALT};
                                        padding: 2px 4px; }}
    QTableWidget#ThresholdTable QHeaderView::section {{
      background: transparent; color: {TEXT_MUTED}; border: none;
      border-bottom: 1px solid {BORDER}; padding: 4px; }}
    QToolTip {{
        background-color: {CARD_BG_ALT};
        color: {TEXT_PRIMARY};
        border: 1px solid {BORDER};
        padding: 4px 6px;
    }}
    QMessageBox {{
        background-color: {CARD_BG};
    }}
    QMessageBox QLabel {{
        color: {TEXT_PRIMARY};
    }}
    """


def apply_dark_theme(app: QApplication) -> None:
    """注入全局深色 QSS + QPalette 兜底（防止某些控件仍走系统色）。"""
    # QPalette 兜底：原生未走 stylesheet 的控件（如 menu、tooltip 默认色）
    pal = app.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(APP_BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Base, QColor(CARD_BG_ALT))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(CARD_BG))
    pal.setColor(QPalette.ColorRole.Text, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Button, QColor(CARD_BG_ALT))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(CARD_BG_ALT))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_PRIMARY))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#2A3548"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(TEXT_PRIMARY))
    app.setPalette(pal)
    app.setStyleSheet(build_qss())


