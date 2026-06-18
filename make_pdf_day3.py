from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY

def create_report():
    doc = SimpleDocTemplate(
        "reports/output/NightShift-June17-2026.pdf",
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
    green = HexColor('#2E7D32')
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

        'highlight': style('highlight',
            fontSize=10, textColor=red,
            fontName='Helvetica-Bold',
            spaceAfter=3, alignment=TA_CENTER),

        'green': style('green',
            fontSize=9, textColor=green,
            fontName='Helvetica-Bold',
            spaceAfter=3, alignment=TA_CENTER),

        'footer': style('footer',
            fontSize=7.5, textColor=grey,
            fontName='Helvetica',
            alignment=TA_CENTER, spaceAfter=2),
    }

    story = []

    # HEADER
    story.append(Paragraph("🌙  THE NIGHT SHIFT", S['title']))
    story.append(Paragraph(
        "Pre-Market Intelligence Report  |  June 17, 2026  |  Delivered before US open",
        S['sub']))
    story.append(HRFlowable(width="100%", thickness=1.5, color=gold))
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "Good morning. While you slept, I watched. Today has one big story.",
        S['intro']))
    story.append(HRFlowable(width="100%", thickness=0.5, color=line))
    story.append(Spacer(1, 4))

    # TWO COLUMNS
    left = []
    right = []

    # LEFT — ASIA
    left.append(Paragraph("🌏  ASIA SIGNAL", S['section']))
    left.append(Paragraph(
        "The semiconductor space sent two conflicting signals overnight. "
        "TSMC dropped 2.34% — a big move that usually means US chip stocks open weak. "
        "But Samsung jumped 2.56% in the same session. Two chip companies, "
        "opposite directions, same night.",
        S['body']))
    left.append(Paragraph(
        "This matters because weakness is TSMC-specific, not a sector collapse. "
        "When the whole chip sector is in trouble, both fall together. "
        "Nikkei up 1.33% confirms risk is ON in Asia today.",
        S['body']))
    left.append(Paragraph(
        "▶ NVDA and AMD are not TSMC. The TSMC drop is not "
        "a warning for US semis today.",
        S['watch']))

    left.append(Spacer(1, 6))

    # LEFT — CRYPTO
    left.append(Paragraph("🔴  CRYPTO RISK GAUGE", S['section']))
    left.append(Paragraph(
        "BTC up 0.3% on a 1.0% day range. ETH flat. "
        "Both calm and confirmed moving same direction. "
        "Crypto is completely silent today.",
        S['body']))
    left.append(Paragraph(
        "▶ No risk signal from crypto. Clean green light.",
        S['watch']))

    left.append(Spacer(1, 6))

    # LEFT — AFTER HOURS
    left.append(Paragraph("💰  AFTER HOURS", S['section']))
    left.append(Paragraph(
        "Quiet across the board. All 5 stocks moved under 0.7%. "
        "No earnings surprises. AMD holding at $510 after hours "
        "confirms the fade is complete. No second move overnight.",
        S['body']))
    left.append(Paragraph(
        "▶ Clean slate. No overnight news carrying into tomorrow.",
        S['watch']))

    # RIGHT — PREMARKET BIG STORY
    right.append(Paragraph("⚡  PREMARKET — THE BIG STORY", S['section']))
    right.append(Paragraph(
        "AMD EUPHORIA FADE — CONFIRMED",
        S['highlight']))
    right.append(Paragraph(
        "AMD gapped up 7.92% premarket. From $506 to $547. "
        "Something specific happened — earnings beat, product news, "
        "or analyst upgrade. Retail traders chased the open.",
        S['body']))
    right.append(Paragraph(
        "What followed is the lesson:",
        S['body']))
    right.append(Paragraph(
        "Premarket: $547  |  Regular Close: $507  |  After Hours: $510",
        S['highlight']))
    right.append(Paragraph(
        "AMD gapped up 7.9% and gave it all back in one session. "
        "This is a textbook EUPHORIA_FADE. Institutions sold into "
        "the retail excitement. Stock closed where it started.",
        S['body']))
    right.append(Paragraph(
        "NVDA followed up 1.8%. QQQ rose 1.69%. "
        "SPY up 0.55%. The whole market got pulled up "
        "by AMD's move — then AMD itself faded.",
        S['body']))
    right.append(Paragraph(
        "▶ Gap ups on normal volume without follow-through "
        "are traps. Your morning read saved you today.",
        S['watch']))

    right.append(Spacer(1, 6))

    # RIGHT — TODAY'S READ
    right.append(Paragraph("🎯  TODAY'S READ", S['section']))
    right.append(Paragraph(
        "AMD taught the pattern book its first real lesson today. "
        "A 7.9% gap with no volume confirmation is not a breakout — "
        "it is a fade setup. This system spotted the gap at premarket "
        "and confirmed the fade after hours. Full cycle. Pattern recorded.",
        S['body']))
    right.append(Paragraph(
        "▶ First confirmed EUPHORIA_FADE in the database. "
        "Watch for this pattern again.",
        S['watch']))

    # TWO COLUMN TABLE
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

    # FOOTER
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
    print("Done: reports/output/NightShift-June17-2026.pdf")

create_report()
