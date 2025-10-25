CREATE TABLE IF NOT EXISTS groups (
    group_id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    user_id BIGSERIAL PRIMARY KEY,
    telegram_id BIGINT UNIQUE NOT NULL,
    username VARCHAR(255),
    first_name VARCHAR(255) NOT NULL,
    last_name VARCHAR(255) NOT NULL,
    role VARCHAR(50) NOT NULL DEFAULT 'pending' CHECK (role IN ('student', 'teacher', 'pending')),
    approved BOOLEAN NOT NULL DEFAULT FALSE,
    registration_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    pending_first_name VARCHAR(255),
    pending_last_name VARCHAR(255),
    name_change_requested_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS students (
    student_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    group_id INT REFERENCES groups(group_id) ON DELETE SET NULL,
    pending_group_id INT REFERENCES groups(group_id) ON DELETE SET NULL,
    group_change_requested_at TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS teachers (
    teacher_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assignments (
    assignment_id SERIAL PRIMARY KEY,
    group_id INT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    file_id VARCHAR(255),
    file_type VARCHAR(50),
    due_date TIMESTAMP WITH TIME ZONE,
    created_by BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    creation_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    accepting_submissions BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id SERIAL PRIMARY KEY,
    assignment_id INT NOT NULL REFERENCES assignments(assignment_id) ON DELETE CASCADE,
    student_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    file_id VARCHAR(255) NOT NULL,
    submission_date TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    is_late BOOLEAN NOT NULL DEFAULT FALSE,
    submitted BOOLEAN NOT NULL DEFAULT TRUE,
    grade INT CHECK (grade >= 0 AND grade <= 20),
    score1 INT CHECK (score1 >= 0 AND score1 <= 10),
    score2 INT CHECK (score2 >= 0 AND score2 <= 10),
    graded_by BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    grade_date TIMESTAMP WITH TIME ZONE,
    teacher_comment TEXT,
    UNIQUE (assignment_id, student_id)
);

CREATE TABLE IF NOT EXISTS attendance (
    attendance_id SERIAL PRIMARY KEY,
    group_id INT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    student_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    attendance_date DATE NOT NULL,
    is_present BOOLEAN NOT NULL DEFAULT TRUE,
    marked_by BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    marked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (group_id, student_id, attendance_date)
);

CREATE TABLE IF NOT EXISTS questions (
    question_id SERIAL PRIMARY KEY,
    student_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    group_id INT REFERENCES groups(group_id) ON DELETE SET NULL,
    question_text TEXT NOT NULL,
    asked_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    answer_text TEXT,
    answered_by BIGINT REFERENCES users(user_id) ON DELETE SET NULL,
    answered_at TIMESTAMP WITH TIME ZONE
);


CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_approved ON users(approved);
CREATE INDEX IF NOT EXISTS idx_students_group_id ON students(group_id);
CREATE INDEX IF NOT EXISTS idx_students_pending_group_id ON students(pending_group_id);
CREATE INDEX IF NOT EXISTS idx_assignments_group_id ON assignments(group_id);
CREATE INDEX IF NOT EXISTS idx_submissions_assignment_id ON submissions(assignment_id);
CREATE INDEX IF NOT EXISTS idx_submissions_student_id ON submissions(student_id);
CREATE INDEX IF NOT EXISTS idx_attendance_group_date ON attendance(group_id, attendance_date);
CREATE INDEX IF NOT EXISTS idx_attendance_student_date ON attendance(student_id, attendance_date);
CREATE INDEX IF NOT EXISTS idx_questions_student_id ON questions(student_id);
CREATE INDEX IF NOT EXISTS idx_questions_group_id ON questions(group_id);
CREATE INDEX IF NOT EXISTS idx_questions_answered_by ON questions(answered_by);
CREATE INDEX IF NOT EXISTS idx_users_pending_name ON users(pending_first_name);