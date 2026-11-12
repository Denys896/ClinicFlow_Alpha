-- ClinicFlow schema (PostgreSQL 16). Applied automatically by docker-compose on first start.
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT         NOT NULL,
    role          VARCHAR(20)  NOT NULL CHECK (role IN ('patient','receptionist','doctor','admin')),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- A patient may have no account (walk-in registered by reception), hence user_id is nullable.
CREATE TABLE patients (
    id            SERIAL PRIMARY KEY,
    user_id       INT UNIQUE REFERENCES users(id) ON DELETE SET NULL,
    full_name     VARCHAR(200) NOT NULL,
    date_of_birth DATE         NOT NULL CHECK (date_of_birth < CURRENT_DATE),
    phone         VARCHAR(30)
);

CREATE TABLE doctors (
    id            SERIAL PRIMARY KEY,
    user_id       INT NOT NULL UNIQUE REFERENCES users(id),
    full_name     VARCHAR(200) NOT NULL,
    specialty     VARCHAR(100) NOT NULL,
    slot_minutes  INT NOT NULL DEFAULT 30 CHECK (slot_minutes IN (15,20,30,60))
);
CREATE INDEX ix_doctors_specialty ON doctors(specialty);

CREATE TABLE working_hours (
    id         SERIAL PRIMARY KEY,
    doctor_id  INT  NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    weekday    INT  NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0 = Monday
    start_time TIME NOT NULL,
    end_time   TIME NOT NULL,
    CHECK (start_time < end_time),
    EXCLUDE USING gist (doctor_id WITH =, weekday WITH =,
                        numrange(EXTRACT(EPOCH FROM start_time)::numeric,
                                 EXTRACT(EPOCH FROM end_time)::numeric) WITH &&)
);

CREATE TABLE time_off (
    id        SERIAL PRIMARY KEY,
    doctor_id INT NOT NULL REFERENCES doctors(id) ON DELETE CASCADE,
    start_at  TIMESTAMPTZ NOT NULL,
    end_at    TIMESTAMPTZ NOT NULL,
    reason    VARCHAR(200),
    CHECK (start_at < end_at)
);
CREATE INDEX ix_time_off_doctor ON time_off(doctor_id, start_at);

CREATE TABLE appointments (
    id          SERIAL PRIMARY KEY,
    doctor_id   INT NOT NULL REFERENCES doctors(id),
    patient_id  INT NOT NULL REFERENCES patients(id),
    start_at    TIMESTAMPTZ NOT NULL,
    end_at      TIMESTAMPTZ NOT NULL,
    status      VARCHAR(20) NOT NULL DEFAULT 'booked'
                CHECK (status IN ('booked','checked_in','completed','cancelled','no_show')),
    created_by  INT NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (start_at < end_at),
    -- No double-booking of a doctor, enforced by the database:
    CONSTRAINT no_doctor_overlap EXCLUDE USING gist
        (doctor_id WITH =, tstzrange(start_at, end_at) WITH &&)
        WHERE (status IN ('booked','checked_in','completed')),
    -- A patient cannot hold two overlapping appointments (FR11):
    CONSTRAINT no_patient_overlap EXCLUDE USING gist
        (patient_id WITH =, tstzrange(start_at, end_at) WITH &&)
        WHERE (status IN ('booked','checked_in','completed'))
);
CREATE INDEX ix_appt_doctor_start  ON appointments(doctor_id, start_at);
CREATE INDEX ix_appt_patient_start ON appointments(patient_id, start_at);

CREATE TABLE visit_notes (
    id             SERIAL PRIMARY KEY,
    appointment_id INT NOT NULL UNIQUE REFERENCES appointments(id),
    diagnosis      TEXT NOT NULL,
    notes          TEXT,
    follow_up      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE notifications (
    id         SERIAL PRIMARY KEY,
    user_id    INT NOT NULL REFERENCES users(id),
    message    TEXT NOT NULL,
    is_read    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_notif_user ON notifications(user_id, is_read);

CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    actor_id    INT REFERENCES users(id),
    action      VARCHAR(50) NOT NULL,
    entity      VARCHAR(50) NOT NULL,
    entity_id   INT NOT NULL,
    details     JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX ix_audit_time   ON audit_log(created_at);
