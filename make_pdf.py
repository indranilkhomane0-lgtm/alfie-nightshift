from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

def create_report():
    doc = SimpleDocTemplate(
        "reports/output/NightShift-June16-2026.pdf",
        pagesize=A4,
        rightMargin=0.6*inch,
        leftMargin=0.6*inch,
        topMargin=0.5*inch,
        bottomMargin=0.5*inch
    )

    gold  = HexColor('#C9A84C')
    dark  = HexColor('#0D0D0D')
    grey  = HexColor('#666666')
    red   = HexColor('#CC3333')
    line  = HexColor('#E0E0E0')

    def style(name, **kw):
        return ParagraphStyle(name, **kw)

    S = {
        'title': style('title',
            fontSize=20, textColor=gold,
            fontName='Helvetica-Bold',
            spaceAfter=2, alignment=TA_LEFT),

        'sub': style('sub',
            fontSize=8, textColor=grey,
            fontName='Helvetica',
            spaceAfter=6, alignment=TA_LEFT),

        'intro': style('intro',
            fontSize=9, textColor=grey,
            fontName='Helvetica-Oblique',
            spaceAfter=6, alignment=TA_CENTER),

        'section': style('section',
            fontSize=9, textColor=gold,
            fontName='Helvetica-Bold',
            spaceBefore=8, spaceAfter=3),

        'body': style('body',
            fontSize=8.5, textColor=dark,
            fontName='Helvetica',
            spaceAfter=3, leading=13,
            alignment=TA_JUSTIFY),

        'watch': style('watch',
            fontSize=8.5, textColor=dark,
            fontName='Helvetica-Bold',
            spaceAfter=2, leading=12,
            leftIndent=8,
            alignment=TA_JUSTIFY),

        'data': style('data',
            fontSize=9, textColor=red,
            fontName='Helvetica-Bold',
            spaceAfter=3, alignment=TA_CENTER),

        'footer': style('footer',
            fontSize=7.5, textColor=grey,
            fontName='Helvetica',
            alignment=TA_CENTER, spaceAfter=2),
    }

    story = []

    # ── HEADER ──────────────────────────────────────────
    story.append(Paragraph("🌙  THE NIGHT SHIFT", S['title']))
    story.append(Paragraph(
        "Pre-Market Intelligence Report  |  June 16, 2026  |  Delivered before US open",
        S['sub']))
    story.append(HRFlowable(width="100%", thickness=1.5, color=gold))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Good morning. While you slept, I watched. Here is what matters today.",
        S['intro']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=line))
    story.append(Spacer(1, 4))

    # ── TWO COLUMN LAYOUT ────────────────────────────────
    # Left: Asia + Crypto   Right: Premarket + After Hours

    left = []
    right = []

    # LEFT — ASIA
    left.append(Paragraph("🌏  ASIA SIGNAL", S['section']))
    left.append(Paragraph(
        "Mixed picture overnight. Nikkei +0.65% and TSMC +0.62% — small but positive. "
        "Hang Seng dropped -1.18%, the one to watch. When Hong Kong sells while Japan "
        "and Taiwan hold, China-related risk is quietly being reduced. Not panic — caution.",
        S['body']))
    left.append(Paragraph(
        "▶ Patient day for anything China-exposed. Do not add to positions.",
        S['watch']))

    left.append(Spacer(1, 6))

    # LEFT — CRYPTO
    left.append(Paragraph("🔴  CRYPTO RISK GAUGE", S['section']))
    left.append(Paragraph(
        "BTC at $65,944. Day range only 1.24% — extremely tight. "
        "ETH down 1.74%, same direction. Signal: CALM and CONFIRMED. "
        "No fear in crypto. If markets sell off today it will not be crypto-driven.",
        S['body']))
    left.append(Paragraph(
        "▶ Crypto is not the risk today. Watch equities, not Bitcoin.",
        S['watch']))

    # RIGHT — PREMARKET
    right.append(Paragraph("⚡  PREMARKET ACTIVITY", S['section']))
    right.append(Paragraph(
        "No unusual volume. All 10 stocks at 1.0x normal. "
        "But price moves are worth noting:",
        S['body']))
    right.append(Paragraph(
        "META -2.49%  |  AMD -2.31%  |  NVDA -1.49%  |  SPY -0.40%",
        S['data']))
    right.append(Paragraph(
        "These stocks rallied hard yesterday — AMD +7.3%, META +4.9%, "
        "NVDA +3.4%. Today's dip is routine profit-taking after strength. "
        "Volume confirms: nobody is running for the exits.",
        S['body']))
    right.append(Paragraph(
        "▶ Low volume dip after strong day = watch for re-entry at open.",
        S['watch']))

    right.append(Spacer(1, 6))

    # RIGHT — AFTER HOURS
    right.append(Paragraph("💰  AFTER HOURS — JUNE 15", S['section']))
    right.append(Paragraph(
        "Completely quiet. All 5 stocks moved under 1%. "
        "No earnings surprises. FOLLOW_THROUGH pattern across the board. "
        "Nothing to act on from yesterday night.",
        S['body']))
    right.append(Paragraph(
        "▶ Clean slate. No overnight news carrying into today.",
        S['watch']))

    # Build two-column table
    col_width = (A4[0] - 1.2*inch) / 2 - 6

    two_col = Table(
        [[left, right]],
        colWidths=[col_width, col_width],
        hAlign='LEFT'
    )
    two_col.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
        ('RIGHTPADDING', (0,0), (-1,-1), 12),
        ('TOPPADDING', (0,0), (-1,-1), 0),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ('LINEAFTER', (0,0), (0,-1), 0.5, line),
        ('LEFTPADDING', (1,0), (1,-1), 12),
    ]))
    story.append(two_col)

    # ── TODAY'S READ ─────────────────────────────────────
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=0.5, color=line))
    story.append(Paragraph("🎯  TODAY'S READ", S['section']))
    story.append(Paragraph(
        "Routine soft open expected. Tech pulled back in premarket after a strong day — "
        "but no institutional selling. Asia is mildly cautious with Hang Seng as the only "
        "real warning signal. Crypto is calm. The setup to watch: AMD and META pulling back "
        "on zero unusual volume after big gains. If they hold their levels in the first "
        "30 minutes — that is a quality re-entry. If they break lower on rising volume — step aside.",
        S['body']))

    # ── FOOTER ───────────────────────────────────────────
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1, color=gold))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "For educational purposes only. Not financial advice.",
        S['footer']))
    story.append(Paragraph(
        "The Night Shift  |  Pre-Market Intelligence  |  Daily before US open",
        S['footer']))

    doc.build(story)
    print("Done: reports/output/NightShift-June16-2026.pdf")

create_report()
