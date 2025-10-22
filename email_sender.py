# email_sender.py - Updated with Template System
import os
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, List, Dict
import asyncio
from concurrent.futures import ThreadPoolExecutor
import asyncpg

# SMTP настройки
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "phe.society.eindhoven@gmail.com")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# Thread pool для асинхронной отправки
executor = ThreadPoolExecutor(max_workers=3)


def _send_email_sync(to_email: str, subject: str, html_body: str, text_body: str = None) -> bool:
    """Синхронная отправка email"""
    try:
        msg = MIMEMultipart('alternative')
        msg['From'] = f"PhE Society <{SMTP_USER}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        
        # Текстовая версия (fallback)
        if text_body:
            part1 = MIMEText(text_body, 'plain', 'utf-8')
            msg.attach(part1)
        
        # HTML версия
        part2 = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(part2)
        
        # Отправка
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        
        print(f"✓ Email sent to {to_email}")
        return True
        
    except Exception as e:
        print(f"✗ Failed to send email to {to_email}: {e}")
        return False


async def send_email(to_email: str, subject: str, html_body: str, text_body: str = None) -> bool:
    """Асинхронная отправка email"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        executor, 
        _send_email_sync, 
        to_email, 
        subject, 
        html_body, 
        text_body
    )


def render_template(template: str, variables: Dict[str, str]) -> str:
    """Render template with variables
    
    Replaces {{variable_name}} with actual values
    """
    result = template
    for key, value in variables.items():
        placeholder = f"{{{{{key}}}}}"
        result = result.replace(placeholder, str(value))
    return result


async def get_email_template(pool: asyncpg.Pool, template_name: str) -> Optional[Dict]:
    """Get email template from database"""
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT * FROM email_templates WHERE name = $1",
            template_name
        )
        if row:
            return dict(row)
    return None


async def send_templated_email(
    pool: asyncpg.Pool,
    template_name: str,
    to_email: str,
    variables: Dict[str, str]
) -> bool:
    """Send email using template from database"""
    template = await get_email_template(pool, template_name)
    
    if not template:
        print(f"Template {template_name} not found")
        return False
    
    # Render subject, HTML and text
    subject = render_template(template['subject'], variables)
    html_body = render_template(template['html_body'], variables)
    text_body = render_template(template['text_body'], variables) if template['text_body'] else None
    
    return await send_email(to_email, subject, html_body, text_body)


def create_random_coffee_email(
    user_name: str,
    match_name: str,
    match_segment: str,
    match_affiliation: str,
    match_about: str,
    match_email: str,
    match_telegram: str,
    starter_questions: List[str]
) -> tuple[str, str]:
    """Создать HTML письмо для Random Coffee (fallback если шаблон не найден)"""
    
    questions_html = '<ul>' + ''.join(f'<li>{q}</li>' for q in starter_questions) + '</ul>'
    questions_text = '\n'.join(f"• {q}" for q in starter_questions)
    
    # Текстовая версия
    text = f"""
Hello {user_name}!

☕ Your Random Coffee Match for This Week

You've been matched with:
👤 {match_name}
🎓 {match_segment}
🏫 {match_affiliation}

Contact:
📧 {match_email}
💬 {match_telegram}

About them:
{match_about}

Starter Questions:
{questions_text}

Reach out and schedule your coffee chat!

Best regards,
PhE Society Team
"""
    
    # HTML версия
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px 10px 0 0;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
        }}
        .content {{
            background: #f8f9fa;
            padding: 30px;
            border-radius: 0 0 10px 10px;
        }}
        .match-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            border-left: 4px solid #667eea;
        }}
        .match-card h2 {{
            margin-top: 0;
            color: #667eea;
        }}
        .info-row {{
            margin: 10px 0;
            padding: 8px 0;
            border-bottom: 1px solid #e9ecef;
        }}
        .info-row:last-child {{
            border-bottom: none;
        }}
        .label {{
            font-weight: 600;
            color: #495057;
        }}
        .contact {{
            background: #e3f2fd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }}
        .questions {{
            background: #fff3cd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }}
        .questions ul {{
            margin: 10px 0;
            padding-left: 20px;
        }}
        .questions li {{
            margin: 8px 0;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #dee2e6;
            color: #6c757d;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>☕ Random Coffee Match</h1>
        <p style="margin: 5px 0 0 0;">Your weekly networking opportunity</p>
    </div>
    
    <div class="content">
        <p>Hello <strong>{user_name}</strong>!</p>
        
        <p>Great news! You've been matched with a new person for this week's Random Coffee. 
        This is a great opportunity to expand your network and learn from someone new.</p>
        
        <div class="match-card">
            <h2>👤 Your Match</h2>
            <div class="info-row">
                <span class="label">Name:</span> {match_name}
            </div>
            <div class="info-row">
                <span class="label">🎓 Segment:</span> {match_segment}
            </div>
            <div class="info-row">
                <span class="label">🏫 Affiliation:</span> {match_affiliation}
            </div>
            <div class="info-row">
                <span class="label">📝 About:</span><br>
                {match_about}
            </div>
        </div>
        
        <div class="contact">
            <h3 style="margin-top: 0;">📲 Contact Information</h3>
            <p style="margin: 5px 0;"><strong>📧 Email:</strong> {match_email}</p>
            <p style="margin: 5px 0;"><strong>💬 Telegram:</strong> {match_telegram}</p>
        </div>
        
        <div class="questions">
            <h3 style="margin-top: 0;">💬 Starter Questions</h3>
            {questions_html}
        </div>
        
        <p style="text-align: center;">
            <strong>Next Step:</strong> Reach out to {match_name.split()[0]} and schedule your coffee chat! ☕
        </p>
    </div>
    
    <div class="footer">
        <p>PhE Society - Photonics Eindhoven</p>
        <p style="font-size: 12px;">
            You're receiving this because you're subscribed to Random Coffee.<br>
            Manage your preferences in the bot.
        </p>
    </div>
</body>
</html>
"""
    
    return html, text


def create_mentorship_email(
    user_name: str,
    user_role: str,  # "mentor" or "mentee"
    match_name: str,
    match_segment: str,
    match_affiliation: str,
    match_about: str,
    match_email: str,
    match_telegram: str
) -> tuple[str, str]:
    """Создать HTML письмо для Mentorship (fallback если шаблон не найден)"""
    
    role_text = "mentor" if user_role == "mentee" else "mentee"
    emoji = "👨‍🏫" if user_role == "mentee" else "👨‍🎓"
    
    next_steps_text = (
        "• Reach out to schedule your first mentoring session\n"
        "• Aim for monthly calls throughout the program\n"
        "• Focus on career development in photonics"
    ) if user_role == "mentor" else (
        "• Your mentor will reach out to you soon\n"
        "• Prepare questions about career in photonics\n"
        "• Be open and engaged in the mentoring process"
    )
    
    next_steps_html = next_steps_text.replace("\n", "<br>")
    
    # Текстовая версия
    text = f"""
Hello {user_name}!

🎓 Mentorship Match Assigned

You have been matched with a {role_text}:

👤 {match_name}
🎓 {match_segment}
🏫 {match_affiliation}

Contact:
📧 {match_email}
💬 {match_telegram}

About them:
{match_about}

Next Steps:
{next_steps_text}

The mentorship program runs through May 31, 2025.

Best regards,
PhE Society Team
"""
    
    # HTML версия
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }}
        .header {{
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            color: white;
            padding: 30px;
            border-radius: 10px 10px 0 0;
            text-align: center;
        }}
        .header h1 {{
            margin: 0;
            font-size: 24px;
        }}
        .content {{
            background: #f8f9fa;
            padding: 30px;
            border-radius: 0 0 10px 10px;
        }}
        .match-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            border-left: 4px solid #f5576c;
        }}
        .match-card h2 {{
            margin-top: 0;
            color: #f5576c;
        }}
        .info-row {{
            margin: 10px 0;
            padding: 8px 0;
            border-bottom: 1px solid #e9ecef;
        }}
        .info-row:last-child {{
            border-bottom: none;
        }}
        .label {{
            font-weight: 600;
            color: #495057;
        }}
        .contact {{
            background: #e3f2fd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }}
        .info-box {{
            background: #d1ecf1;
            border-left: 4px solid #0c5460;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }}
        .footer {{
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #dee2e6;
            color: #6c757d;
            font-size: 14px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎓 Mentorship Match</h1>
        <p style="margin: 5px 0 0 0;">Your mentorship journey begins!</p>
    </div>
    
    <div class="content">
        <p>Hello <strong>{user_name}</strong>!</p>
        
        <p>Exciting news! You have been matched in our Mentorship Program.</p>
        
        <div class="match-card">
            <h2>{emoji} Your {role_text.capitalize()}</h2>
            <div class="info-row">
                <span class="label">Name:</span> {match_name}
            </div>
            <div class="info-row">
                <span class="label">🎓 Segment:</span> {match_segment}
            </div>
            <div class="info-row">
                <span class="label">🏫 Affiliation:</span> {match_affiliation}
            </div>
            <div class="info-row">
                <span class="label">📝 About:</span><br>
                {match_about}
            </div>
        </div>
        
        <div class="contact">
            <h3 style="margin-top: 0;">📲 Contact Information</h3>
            <p style="margin: 5px 0;"><strong>📧 Email:</strong> {match_email}</p>
            <p style="margin: 5px 0;"><strong>💬 Telegram:</strong> {match_telegram}</p>
        </div>
        
        <div class="info-box">
            <strong>Next Steps:</strong><br>
            {next_steps_html}
        </div>
        
        <p style="text-align: center; margin-top: 20px;">
            <strong>Program Duration:</strong> Through May 31, 2025<br>
            <strong>Frequency:</strong> ~Monthly calls
        </p>
    </div>
    
    <div class="footer">
        <p>PhE Society - Photonics Eindhoven</p>
        <p style="font-size: 12px;">
            Questions about the mentorship program? Reply to this email.
        </p>
    </div>
</body>
</html>
"""
    
    return html, text