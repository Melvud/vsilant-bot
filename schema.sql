-- =====================================================================
-- PhE Bot — Full Database Schema (PostgreSQL)
-- Complete working version with all features
-- =====================================================================

BEGIN;

-- Drop existing tables (careful in production!)
DROP TABLE IF EXISTS email_templates CASCADE;
DROP TABLE IF EXISTS broadcasts CASCADE;
DROP TABLE IF EXISTS mentorship_matches CASCADE;
DROP TABLE IF EXISTS mentorship_mentees CASCADE;
DROP TABLE IF EXISTS mentorship_mentors CASCADE;
DROP TABLE IF EXISTS event_rsvps CASCADE;
DROP TABLE IF EXISTS events CASCADE;
DROP TABLE IF EXISTS approvals_log CASCADE;
DROP TABLE IF EXISTS run_logs CASCADE;
DROP TABLE IF EXISTS app_settings CASCADE;
DROP TABLE IF EXISTS weekly_matches CASCADE;
DROP TABLE IF EXISTS pairings CASCADE;
DROP TABLE IF EXISTS users CASCADE;

-- ---[ Users ]----------------------------------------------------------
CREATE TABLE users(
    user_id            BIGINT PRIMARY KEY,
    username           TEXT,
    full_name          TEXT,
    display_name       TEXT,
    email              TEXT,
    segment            TEXT,
    affiliation        TEXT,
    about              TEXT,
    mentor_flag        BOOLEAN DEFAULT FALSE,
    communication_mode TEXT DEFAULT 'email+telegram',
    status             TEXT DEFAULT 'pending',
    subscribed         BOOLEAN DEFAULT FALSE,
    rc_frequency       TEXT DEFAULT 'weekly',
    rc_pref_tue        BOOLEAN DEFAULT TRUE,
    rc_pref_universities BOOLEAN DEFAULT FALSE,
    rc_pref_industry   BOOLEAN DEFAULT FALSE,
    socials_opt_in     BOOLEAN DEFAULT FALSE,
    notif_announcements BOOLEAN DEFAULT TRUE,
    notif_events       BOOLEAN DEFAULT TRUE,
    notif_rc           BOOLEAN DEFAULT TRUE,
    notif_mentor       BOOLEAN DEFAULT TRUE,
    notif_socials      BOOLEAN DEFAULT TRUE,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ,
    consent_ts         TIMESTAMPTZ,
    CONSTRAINT users_communication_mode_chk 
        CHECK (communication_mode IS NULL OR communication_mode IN ('email_only','telegram_only','email+telegram')),
    CONSTRAINT users_status_chk 
        CHECK (status IN ('pending','approved','rejected','deletion_requested','left'))
);

-- Indexes for users
CREATE INDEX idx_users_email_lower ON users ((lower(email)));
CREATE INDEX idx_users_status ON users (status);
CREATE INDEX idx_users_subscribed ON users (subscribed);
CREATE INDEX idx_users_comms ON users (communication_mode);
CREATE INDEX idx_users_mentor_flag ON users (mentor_flag);

-- ---[ Pairing history & weekly matches ]-------------------------------
CREATE TABLE pairings(
    user_a          BIGINT NOT NULL,
    user_b          BIGINT NOT NULL,
    last_matched_at TIMESTAMPTZ,
    PRIMARY KEY(user_a, user_b),
    CONSTRAINT pairings_order_chk CHECK (user_a < user_b)
);

CREATE TABLE weekly_matches(
    week_date DATE NOT NULL,
    user_a   BIGINT NOT NULL,
    user_b   BIGINT NOT NULL,
    PRIMARY KEY(week_date, user_a, user_b)
);

-- ---[ Run logs & app settings ]---------------------------------------
CREATE TABLE run_logs(
    id           BIGSERIAL PRIMARY KEY,
    run_type     TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    pairs_count  INT,
    triggered_by BIGINT,
    status       TEXT,
    error_text   TEXT
);

CREATE INDEX idx_run_logs_started_at ON run_logs (started_at DESC);

CREATE TABLE app_settings(
    id            SMALLINT PRIMARY KEY DEFAULT 1,
    schedule_days TEXT[],
    schedule_time TEXT,
    last_run_at   TIMESTAMPTZ,
    CONSTRAINT only_one_row CHECK (id = 1)
);

-- Seed default settings
INSERT INTO app_settings(id, schedule_days, schedule_time)
VALUES (1, ARRAY['MON'], '09:00');

-- ---[ Approvals log ]--------------------------------------------------
CREATE TABLE approvals_log(
    id        BIGSERIAL PRIMARY KEY,
    user_id   BIGINT NOT NULL,
    action    TEXT NOT NULL,
    by_admin  BIGINT,
    ts        TIMESTAMPTZ DEFAULT NOW(),
    note      TEXT
);

CREATE INDEX idx_approvals_log_user ON approvals_log (user_id);
CREATE INDEX idx_approvals_log_ts ON approvals_log (ts DESC);

-- ---[ Events & RSVPs ]-------------------------------------------------
CREATE TABLE events(
    id             BIGSERIAL PRIMARY KEY,
    title          TEXT NOT NULL,
    description    TEXT,
    location       TEXT,
    starts_at      TIMESTAMPTZ,
    ends_at        TIMESTAMPTZ,
    capacity       INT,
    rsvp_open_at   TIMESTAMPTZ,
    rsvp_close_at  TIMESTAMPTZ,
    status         TEXT DEFAULT 'draft',
    event_type     TEXT DEFAULT 'event',
    photo_url      TEXT,
    registration_url TEXT,
    created_by     BIGINT,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    updated_at     TIMESTAMPTZ,
    broadcasted_at TIMESTAMPTZ,
    CONSTRAINT events_status_chk 
        CHECK (status IN ('draft','published','archived')),
    CONSTRAINT events_type_chk 
        CHECK (event_type IN ('event','social'))
);

CREATE INDEX idx_events_starts_at ON events (starts_at);
CREATE INDEX idx_events_status ON events (status);
CREATE INDEX idx_events_type ON events (event_type);
CREATE INDEX idx_events_broadcasted ON events (broadcasted_at);

CREATE TABLE event_rsvps(
    event_id   BIGINT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    status     TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY(event_id, user_id),
    CONSTRAINT event_rsvps_status_chk 
        CHECK (status IN ('going','cant','maybe'))
);

CREATE INDEX idx_event_rsvps_event ON event_rsvps (event_id);
CREATE INDEX idx_event_rsvps_user ON event_rsvps (user_id);

-- ---[ Mentorship pools & matches ]------------------------------------
CREATE TABLE mentorship_mentors(
    user_id       BIGINT PRIMARY KEY,
    tags          TEXT[],
    monthly_avail INT DEFAULT 1,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_mentors_tags ON mentorship_mentors USING GIN (tags);

CREATE TABLE mentorship_mentees(
    user_id    BIGINT PRIMARY KEY,
    interests  TEXT[],
    pref       TEXT,
    availability_window TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_mentees_interests ON mentorship_mentees USING GIN (interests);

CREATE TABLE mentorship_matches(
    mentor_id  BIGINT NOT NULL,
    mentee_id  BIGINT NOT NULL,
    matched_at TIMESTAMPTZ DEFAULT NOW(),
    active     BOOLEAN DEFAULT TRUE,
    PRIMARY KEY(mentor_id, mentee_id)
);

CREATE INDEX idx_mentorship_matches_active ON mentorship_matches (active);
CREATE INDEX idx_mentorship_matches_mentor ON mentorship_matches (mentor_id);
CREATE INDEX idx_mentorship_matches_mentee ON mentorship_matches (mentee_id);

-- ---[ Broadcasts ]-----------------------------------------------------
CREATE TABLE broadcasts(
    id                 BIGSERIAL PRIMARY KEY,
    title              TEXT,
    body               TEXT,
    segment_filter     TEXT[],
    affiliation_filter TEXT[],
    program_filter     TEXT[],
    created_by         BIGINT,
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    sent_to            INT DEFAULT 0
);

CREATE INDEX idx_broadcasts_created ON broadcasts (created_at DESC);

-- ---[ Email Templates ]-----------------------------------------------
CREATE TABLE email_templates(
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    subject     TEXT NOT NULL,
    html_body   TEXT NOT NULL,
    text_body   TEXT,
    description TEXT,
    variables   TEXT[],
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_email_templates_name ON email_templates (name);

-- Seed default email templates
INSERT INTO email_templates (name, subject, html_body, text_body, description, variables)
VALUES (
    'random_coffee',
    '☕ Your Random Coffee Match This Week',
    '<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, ''Segoe UI'', Roboto, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px 10px 0 0;
            text-align: center;
        }
        .header h1 {
            margin: 0;
            font-size: 24px;
        }
        .content {
            background: #f8f9fa;
            padding: 30px;
            border-radius: 0 0 10px 10px;
        }
        .match-card {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            border-left: 4px solid #667eea;
        }
        .match-card h2 {
            margin-top: 0;
            color: #667eea;
        }
        .info-row {
            margin: 10px 0;
            padding: 8px 0;
            border-bottom: 1px solid #e9ecef;
        }
        .info-row:last-child {
            border-bottom: none;
        }
        .label {
            font-weight: 600;
            color: #495057;
        }
        .contact {
            background: #e3f2fd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }
        .questions {
            background: #fff3cd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }
        .questions ul {
            margin: 10px 0;
            padding-left: 20px;
        }
        .questions li {
            margin: 8px 0;
        }
        .footer {
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #dee2e6;
            color: #6c757d;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>☕ Random Coffee Match</h1>
        <p style="margin: 5px 0 0 0;">Your weekly networking opportunity</p>
    </div>
    
    <div class="content">
        <p>Hello <strong>{{user_name}}</strong>!</p>
        
        <p>Great news! You''ve been matched with a new person for this week''s Random Coffee. 
        This is a great opportunity to expand your network and learn from someone new.</p>
        
        <div class="match-card">
            <h2>👤 Your Match</h2>
            <div class="info-row">
                <span class="label">Name:</span> {{match_name}}
            </div>
            <div class="info-row">
                <span class="label">🎓 Segment:</span> {{match_segment}}
            </div>
            <div class="info-row">
                <span class="label">🏫 Affiliation:</span> {{match_affiliation}}
            </div>
            <div class="info-row">
                <span class="label">📝 About:</span><br>
                {{match_about}}
            </div>
        </div>
        
        <div class="contact">
            <h3 style="margin-top: 0;">📲 Contact Information</h3>
            <p style="margin: 5px 0;"><strong>📧 Email:</strong> {{match_email}}</p>
            <p style="margin: 5px 0;"><strong>💬 Telegram:</strong> {{match_telegram}}</p>
        </div>
        
        <div class="questions">
            <h3 style="margin-top: 0;">💬 Starter Questions</h3>
            {{starter_questions}}
        </div>
        
        <p style="text-align: center;">
            <strong>Next Step:</strong> Reach out and schedule your coffee chat! ☕
        </p>
    </div>
    
    <div class="footer">
        <p>PhE Society - Photonics Eindhoven</p>
        <p style="font-size: 12px;">
            You''re receiving this because you''re subscribed to Random Coffee.<br>
            Manage your preferences in the bot.
        </p>
    </div>
</body>
</html>',
    'Hello {{user_name}}!

☕ Your Random Coffee Match for This Week

You''ve been matched with:
👤 {{match_name}}
🎓 {{match_segment}}
🏫 {{match_affiliation}}

Contact:
📧 {{match_email}}
💬 {{match_telegram}}

About them:
{{match_about}}

Starter Questions:
{{starter_questions}}

Reach out and schedule your coffee chat!

Best regards,
PhE Society Team',
    'Template for Random Coffee weekly match notifications',
    ARRAY['user_name', 'match_name', 'match_segment', 'match_affiliation', 'match_about', 'match_email', 'match_telegram', 'starter_questions']
);

INSERT INTO email_templates (name, subject, html_body, text_body, description, variables)
VALUES (
    'mentorship_match',
    '🎓 Mentorship Match - {{user_role_title}}',
    '<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, ''Segoe UI'', Roboto, Arial, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 600px;
            margin: 0 auto;
            padding: 20px;
        }
        .header {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            color: white;
            padding: 30px;
            border-radius: 10px 10px 0 0;
            text-align: center;
        }
        .header h1 {
            margin: 0;
            font-size: 24px;
        }
        .content {
            background: #f8f9fa;
            padding: 30px;
            border-radius: 0 0 10px 10px;
        }
        .match-card {
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin: 20px 0;
            border-left: 4px solid #f5576c;
        }
        .match-card h2 {
            margin-top: 0;
            color: #f5576c;
        }
        .info-row {
            margin: 10px 0;
            padding: 8px 0;
            border-bottom: 1px solid #e9ecef;
        }
        .info-row:last-child {
            border-bottom: none;
        }
        .label {
            font-weight: 600;
            color: #495057;
        }
        .contact {
            background: #e3f2fd;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }
        .info-box {
            background: #d1ecf1;
            border-left: 4px solid #0c5460;
            padding: 15px;
            border-radius: 6px;
            margin: 15px 0;
        }
        .footer {
            text-align: center;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #dee2e6;
            color: #6c757d;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>🎓 Mentorship Match</h1>
        <p style="margin: 5px 0 0 0;">Your mentorship journey begins!</p>
    </div>
    
    <div class="content">
        <p>Hello <strong>{{user_name}}</strong>!</p>
        
        <p>Exciting news! You have been matched in our Mentorship Program.</p>
        
        <div class="match-card">
            <h2>{{match_emoji}} Your {{match_role}}</h2>
            <div class="info-row">
                <span class="label">Name:</span> {{match_name}}
            </div>
            <div class="info-row">
                <span class="label">🎓 Segment:</span> {{match_segment}}
            </div>
            <div class="info-row">
                <span class="label">🏫 Affiliation:</span> {{match_affiliation}}
            </div>
            <div class="info-row">
                <span class="label">📝 About:</span><br>
                {{match_about}}
            </div>
        </div>
        
        <div class="contact">
            <h3 style="margin-top: 0;">📲 Contact Information</h3>
            <p style="margin: 5px 0;"><strong>📧 Email:</strong> {{match_email}}</p>
            <p style="margin: 5px 0;"><strong>💬 Telegram:</strong> {{match_telegram}}</p>
        </div>
        
        <div class="info-box">
            <strong>Next Steps:</strong><br>
            {{next_steps}}
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
</html>',
    'Hello {{user_name}}!

🎓 Mentorship Match Assigned

You have been matched with a {{match_role}}:

👤 {{match_name}}
🎓 {{match_segment}}
🏫 {{match_affiliation}}

Contact:
📧 {{match_email}}
💬 {{match_telegram}}

About them:
{{match_about}}

Next Steps:
{{next_steps}}

The mentorship program runs through May 31, 2025.

Best regards,
PhE Society Team',
    'Template for mentorship match notifications',
    ARRAY['user_name', 'user_role_title', 'match_emoji', 'match_role', 'match_name', 'match_segment', 'match_affiliation', 'match_about', 'match_email', 'match_telegram', 'next_steps']
);

COMMIT;

-- Verification queries
SELECT 'Users table created' as status, COUNT(*) as count FROM users;
SELECT 'Events table created' as status, COUNT(*) as count FROM events;
SELECT 'Email templates created' as status, COUNT(*) as count FROM email_templates;
SELECT 'App settings created' as status, COUNT(*) as count FROM app_settings;

-- Show all tables
SELECT table_name 
FROM information_schema.tables 
WHERE table_schema = 'public' 
ORDER BY table_name;